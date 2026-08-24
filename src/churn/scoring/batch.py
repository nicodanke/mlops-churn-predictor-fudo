"""Job de scoring batch mensual.

Corre una vez al mes, sobre el snapshot mas reciente, y deja el resultado escrito en
disco (parquet + JSON). La API no ejecuta el modelo: solo lee lo que este job dejo.
Esa separacion es lo que permite que la capa de aplicacion sea liviana y barata de
hostear, y que el batch pueda correr en un Cloud Run Job programado.

Salida por cuenta:
    probabilidad de churn calibrada
    revenue mensual segun el plan contratado
    revenue en riesgo y categoria (alto / medio / bajo / no_churn)
    top-N features que empujaron la prediccion, con valor y percentil (mapa de calor)
    un texto de diagnostico listo para mostrar
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from churn.config import Config
from churn.data.loader import next_period
from churn.data.seasonality import account_seasonality
from churn.explain.narrative import build_narrative, build_narrative_llm
from churn.explain.shap_explainer import ShapExplainer, global_importance_from_shap
from churn.models.artifact import ModelArtifact
from churn.pricing.plans import parse_plan
from churn.pricing.revenue import PricingBook, revenue_for_plan
from churn.scoring.risk import RiskBands, assign_risk, risk_summary

logger = logging.getLogger(__name__)


def score_period(
    features: pd.DataFrame,
    artifact: ModelArtifact,
    cfg: Config,
    periodo: int | None = None,
    pricing: PricingBook | None = None,
    narrative_mode: str = "rules",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Puntua todas las cuentas activas de un periodo. Por defecto, el mas reciente."""
    periodo = int(periodo or features["periodo"].max())
    batch = features[features["periodo"] == periodo].copy().reset_index(drop=True)
    if batch.empty:
        raise ValueError(f"No hay cuentas en el periodo {periodo}")

    target_period = next_period(periodo)
    logger.info(
        "Scoreando %s cuentas del periodo %s (prediccion de baja en %s)",
        f"{len(batch):,}",
        periodo,
        target_period,
    )

    X = batch[artifact.feature_names]
    probabilities = artifact.predict_proba(X)

    pricing = pricing or PricingBook.load()
    revenue = _revenue_column(batch, pricing)

    bands = RiskBands.from_config(cfg, fallback_threshold=artifact.decision_threshold)
    risk = assign_risk(probabilities, revenue["monthly_revenue"], bands)

    explainer = ShapExplainer(artifact, top_n=int(cfg.get("explain.top_n_features", 8)))
    shap_values, contributions = explainer.explain(X)

    narratives = _build_narratives(
        batch, risk, contributions, revenue, pricing.currency, narrative_mode
    )

    scored = pd.concat(
        [
            batch[["id", "periodo", "nombre", "plan", "pais", "estado"]].reset_index(drop=True),
            risk,
            revenue.drop(columns=["monthly_revenue"]),
            _seasonality_flags(features, batch, periodo),
        ],
        axis=1,
    )
    scored["periodo_prediccion"] = target_period
    scored["top_features"] = [[c.to_dict() for c in row] for row in contributions]
    scored["diagnostico"] = narratives
    scored = scored.sort_values("revenue_at_risk", ascending=False).reset_index(drop=True)

    metadata = {
        "periodo_snapshot": periodo,
        "periodo_prediccion": target_period,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_accounts": int(len(scored)),
        "model_trained_at": artifact.trained_at,
        "model_metrics": artifact.metrics,
        "decision_threshold": bands.no_churn_threshold,
        "risk_bands": {"alto": bands.alto, "medio": bands.medio},
        "currency": pricing.currency,
        "pricing_loaded": pricing.is_filled,
        "narrative_mode": narrative_mode,
        "summary": risk_summary(scored).to_dict(orient="records"),
        "global_importance": global_importance_from_shap(
            shap_values, artifact.feature_names
        ).to_dict(orient="records"),
    }

    if not pricing.is_filled:
        logger.warning(
            "La lista de precios en config/pricing.yaml esta vacia: el revenue en riesgo "
            "va a dar 0 y todas las cuentas con churn predicho caen en categoria 'bajo'. "
            "Completa los precios y volve a correr el scoring."
        )

    return scored, metadata


def _revenue_column(batch: pd.DataFrame, pricing: PricingBook) -> pd.DataFrame:
    """Revenue mensual por cuenta, cacheando el calculo por plan code distinto."""
    codes = batch["plan"].astype("string").fillna("")
    per_code = {}
    for code in codes.unique():
        breakdown = revenue_for_plan(code, pricing, parse_plan(code))
        per_code[code] = {
            "monthly_revenue": breakdown.total,
            "revenue_desglose": json.dumps(breakdown.items, ensure_ascii=False),
            "plan_descripcion": parse_plan(code).describe(),
            "precio_incompleto": not breakdown.is_complete,
        }
    return pd.DataFrame([per_code[c] for c in codes]).reset_index(drop=True)


def _build_narratives(
    batch: pd.DataFrame,
    risk: pd.DataFrame,
    contributions: list,
    revenue: pd.DataFrame,
    currency: str,
    mode: str,
) -> list[str]:
    """Texto por cuenta. El modo `llm` se reserva para las cuentas de riesgo alto."""
    out = []
    for i in range(len(batch)):
        prob = float(risk.loc[i, "churn_probability"])
        category = str(risk.loc[i, "risk_category"])
        rev = float(revenue.loc[i, "monthly_revenue"])

        if mode == "llm" and category == "alto":
            text = build_narrative_llm(
                contributions[i],
                prob,
                category,
                account_name=batch.loc[i, "nombre"],
                revenue=rev,
                currency=currency,
            )
        else:
            text = build_narrative(contributions[i], prob, category, rev, currency)
        out.append(text)
    return out


def write_predictions(
    scored: pd.DataFrame,
    metadata: dict[str, Any],
    output_dir: str | Path,
    formats: list[str] | None = None,
) -> dict[str, Path]:
    """Persiste el resultado del batch. El parquet es la fuente, el JSON lo lee la API."""
    formats = formats or ["parquet", "json"]
    out_dir = Path(output_dir) / str(metadata["periodo_snapshot"])
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    if "parquet" in formats:
        path = out_dir / "predictions.parquet"
        table = scored.copy()
        table["top_features"] = table["top_features"].map(
            lambda v: json.dumps(v, ensure_ascii=False)
        )
        table["risk_category"] = table["risk_category"].astype(str)
        table.to_parquet(path, index=False)
        written["parquet"] = path

    if "json" in formats:
        path = out_dir / "predictions.json"
        records = scored.copy()
        records["risk_category"] = records["risk_category"].astype(str)
        with open(path, "w") as fh:
            json.dump(
                {"metadata": metadata, "predictions": records.to_dict(orient="records")},
                fh,
                ensure_ascii=False,
                default=str,
            )
        written["json"] = path

    meta_path = out_dir / "metadata.json"
    with open(meta_path, "w") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False, default=str)
    written["metadata"] = meta_path

    # Puntero al ultimo batch, para que la API no tenga que adivinar el periodo.
    latest = Path(output_dir) / "latest.json"
    with open(latest, "w") as fh:
        json.dump({"periodo": metadata["periodo_snapshot"], "path": str(out_dir)}, fh, indent=2)
    written["latest"] = latest

    logger.info("Predicciones escritas en %s", out_dir)
    return written


def _seasonality_flags(
    features: pd.DataFrame, batch: pd.DataFrame, periodo: int
) -> pd.DataFrame:
    """Historial de pausas de cada cuenta, mirando solo hasta el mes que se scorea.

    Es la version causal de la deteccion de estacionalidad: el filtro que se usa para
    limpiar el entrenamiento mira el panel entero (incluido el futuro de cada fila), y
    eso en produccion no existe. Aca solo se cuenta lo que ya paso.

    Le sirve al equipo de CX para leer la prediccion con contexto: una cuenta con dos
    pausas previas que vuelve a caer en mayo probablemente sea un negocio de temporada,
    no una baja que haya que salir a rescatar.
    """
    historico = features[features["periodo"] <= periodo]
    perfil = account_seasonality(historico)

    cuentas = batch["id"]
    if perfil.empty:
        pausas = pd.Series(0, index=cuentas.index)
        meses = pd.Series(pd.NA, index=cuentas.index, dtype="Int64")
        estacional = pd.Series(False, index=cuentas.index)
    else:
        indexado = perfil.set_index("id")
        # reindex() sobre los ids del batch mete NaN donde la cuenta nunca pauso, y eso
        # empuja la columna a float: el mes tiene que volver a entero (nullable) o el
        # JSON termina diciendo "mayo" con un 5.0.
        buscar = lambda col: indexado[col].reindex(cuentas).reset_index(drop=True)  # noqa: E731
        pausas = buscar("n_pausas").fillna(0)
        meses = buscar("mes_baja_modal").astype("Float64").astype("Int64")
        estacional = buscar("es_estacional").fillna(False)

    salida = pd.DataFrame(
        {
            "pausas_historicas": pausas.astype(int),
            "mes_de_pausa_habitual": meses,
            "posible_estacional": estacional.astype(bool),
        }
    )
    return salida.reset_index(drop=True)
