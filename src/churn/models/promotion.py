"""Champion / challenger: decide si un modelo recien entrenado reemplaza al de produccion.

El campeon es el modelo que genera las predicciones que ve CX. Cada reentrenamiento
produce un candidato, y el candidato solo pasa a produccion si:

  1. supera el umbral de negocio (`evaluation.min_pr_auc`), y
  2. le gana al campeon por al menos `promotion.min_improvement`, y
  3. no empeora mas de lo tolerado ninguna metrica de `promotion.max_regression`.

La comparacion se hace sobre el MISMO test. Las metricas guardadas en cada artefacto no
alcanzan: un campeon entrenado hace dos meses se evaluo sobre otros periodos, y con un
churn base que se mueve de mes a mes, la diferencia de PR-AUC entre dos tests distintos
puede ser puro cambio de muestra. Por eso el campeon se vuelve a scorear sobre el test
del candidato, y recien ahi se comparan. Si el campeon ya no se puede evaluar (el feature
engineering cambio y le faltan columnas) se cae a sus metricas guardadas y la decision
lo deja anotado.

Layout que asume `promote`, igual en disco local que en el bucket montado:

    models/
      candidates/<version>/     un directorio por reentrenamiento, con su decision.json
      champion/                 copia del modelo en produccion, con release.json
      releases/<version>/       copia permanente de cada version que fue campeona
      registry.json             historial de promociones
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from churn.config import Config
from churn.models.artifact import METADATA_FILENAME, MODEL_FILENAME, ModelArtifact, load_model
from churn.models.evaluate import EvaluationResult, evaluate_predictions
from churn.models.split import holdout_frame

logger = logging.getLogger(__name__)

DECISION_FILENAME = "decision.json"
DECISION_REPORT_FILENAME = "decision.md"
RELEASE_FILENAME = "release.json"
REGISTRY_FILENAME = "registry.json"

# Metricas donde mas alto es mejor. El Brier queda afuera a proposito: compararlo pediria
# invertir el sentido de la mejora y de la regresion.
COMPARABLE_METRICS = ("pr_auc", "roc_auc", "best_f1", "lift_over_baseline")

# Como se obtuvieron las metricas del campeon.
SIN_CAMPEON = "sin_campeon"
MISMO_TEST = "mismo_test"
METRICAS_GUARDADAS = "metricas_guardadas"

_COMPARACION_TEXTO = {
    SIN_CAMPEON: "No hay modelo en produccion: alcanza con pasar el umbral de negocio.",
    MISMO_TEST: "El campeon se re-evaluo sobre el mismo test que el candidato.",
    METRICAS_GUARDADAS: (
        "El campeon no se pudo re-evaluar con las features actuales; se usan las metricas "
        "de su propio test, que no es el mismo. Tomar la comparacion con cuidado."
    ),
}

# Tolerancia para no decidir por el ultimo decimal de un redondeo.
_EPS = 1e-9


@dataclass
class PromotionPolicy:
    metric: str = "pr_auc"
    min_pr_auc: float = 0.20
    min_improvement: float = 0.0
    max_regression: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Config) -> PromotionPolicy:
        policy = cls(
            metric=str(cfg.get("promotion.metric", "pr_auc")),
            min_pr_auc=float(cfg.get("evaluation.min_pr_auc", 0.20)),
            min_improvement=float(cfg.get("promotion.min_improvement", 0.0)),
            max_regression={
                str(k): float(v) for k, v in (cfg.get("promotion.max_regression") or {}).items()
            },
        )
        for metric in (policy.metric, *policy.max_regression):
            if metric not in COMPARABLE_METRICS:
                raise ValueError(
                    f"promotion: '{metric}' no es comparable. Opciones: {COMPARABLE_METRICS}"
                )
        return policy


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Decision:
    promote: bool
    version: str
    candidate: dict[str, Any]
    champion: dict[str, Any] | None
    champion_version: str | None
    comparison: str
    checks: list[Check]
    forced: bool = False
    decided_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    @property
    def reason(self) -> str:
        fallidos = [c for c in self.checks if not c.passed]
        if not fallidos:
            return "El candidato cumple todos los criterios."
        if self.promote:
            return "Promocion forzada: " + "; ".join(c.detail for c in fallidos)
        return "; ".join(c.detail for c in fallidos)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["reason"] = self.reason
        return out

    def to_markdown(self) -> str:
        resultado = "se promueve a produccion" if self.promote else "NO se promueve"
        lines = [
            f"### Champion / challenger: {resultado}",
            "",
            f"Candidato `{self.version or '-'}` contra campeon `{self.champion_version or '-'}`.",
            _COMPARACION_TEXTO.get(self.comparison, ""),
            "",
            "| Metrica (test) | Candidato | Campeon |",
            "|---|---|---|",
        ]
        for metric in ("pr_auc", "roc_auc", "best_f1", "brier"):
            cand = self.candidate.get(metric)
            champ = (self.champion or {}).get(metric)
            lines.append(f"| {metric} | {_fmt(cand)} | {_fmt(champ)} |")
        lines += ["", "| Criterio | | Detalle |", "|---|---|---|"]
        for check in self.checks:
            lines.append(f"| {check.name} | {'ok' if check.passed else 'falla'} | {check.detail} |")
        if self.forced:
            lines += ["", "_Se pidio promover aunque no mejore al campeon._"]
        return "\n".join(lines) + "\n"


def decide(
    candidate: dict[str, Any],
    champion: dict[str, Any] | None,
    policy: PromotionPolicy,
    *,
    version: str = "",
    champion_version: str | None = None,
    comparison: str = SIN_CAMPEON,
    force: bool = False,
) -> Decision:
    """Aplica la politica a las metricas de test de ambos modelos.

    `force` saltea la comparacion contra el campeon (sirve para publicar un arreglo de
    scoring que no mueve las metricas) pero nunca el umbral de negocio.
    """
    pr_auc = float(candidate["pr_auc"])
    checks = [
        Check(
            "umbral_negocio",
            pr_auc >= policy.min_pr_auc - _EPS,
            f"PR-AUC {pr_auc:.4f} contra el minimo acordado {policy.min_pr_auc:.2f}",
        )
    ]

    if champion is not None:
        metric = policy.metric
        delta = float(candidate[metric]) - float(champion[metric])
        checks.append(
            Check(
                "mejora",
                delta >= policy.min_improvement - _EPS,
                f"{metric} {candidate[metric]:.4f} contra {champion[metric]:.4f} "
                f"(diferencia {delta:+.4f}, se pide {policy.min_improvement:+.4f})",
            )
        )
        for name, tolerance in policy.max_regression.items():
            drop = float(champion[name]) - float(candidate[name])
            checks.append(
                Check(
                    f"regresion_{name}",
                    drop <= tolerance + _EPS,
                    f"{name} cae {max(drop, 0.0):.4f} (se tolera hasta {tolerance:.4f})",
                )
            )

    supera_umbral = checks[0].passed
    le_gana_al_campeon = all(c.passed for c in checks[1:])
    return Decision(
        promote=supera_umbral and (le_gana_al_campeon or force),
        version=version,
        candidate=candidate,
        champion=champion,
        champion_version=champion_version,
        comparison=comparison if champion is not None else SIN_CAMPEON,
        checks=checks,
        forced=force,
    )


def evaluate_artifact(
    features: pd.DataFrame, cfg: Config, artifact: ModelArtifact
) -> EvaluationResult:
    """Evalua un modelo cualquiera sobre el test que define la configuracion actual."""
    test = holdout_frame(
        features,
        n_test_periods=int(cfg.get("split.n_test_periods", 2)),
        n_val_periods=int(cfg.get("split.n_val_periods", 2)),
        min_months_in_panel=int(cfg.get("labeling.min_months_in_panel", 2)),
    )
    probabilities = artifact.predict_proba(test[artifact.feature_names])
    top_k = list(cfg.get("evaluation.top_k_fractions", [0.01, 0.05, 0.10, 0.20]))
    return evaluate_predictions(test["churn"].to_numpy().astype(int), probabilities, top_k)


def challenge(
    features: pd.DataFrame,
    cfg: Config,
    candidate: ModelArtifact,
    champion_dir: str | Path | None,
    *,
    version: str = "",
    force: bool = False,
) -> Decision:
    """Compara un candidato recien entrenado contra el campeon y decide."""
    champion_metrics, champion_version, comparison = _champion_metrics(features, cfg, champion_dir)
    decision = decide(
        candidate.metrics["test"],
        champion_metrics,
        PromotionPolicy.from_config(cfg),
        version=version,
        champion_version=champion_version,
        comparison=comparison,
        force=force,
    )
    logger.info(
        "Decision: %s. %s", "promover" if decision.promote else "no promover", decision.reason
    )
    return decision


def write_decision(decision: Decision, candidate_dir: str | Path) -> Path:
    """Deja la decision al lado del candidato, en JSON para CI y en markdown para leer."""
    out = Path(candidate_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / DECISION_FILENAME, decision.to_dict())
    (out / DECISION_REPORT_FILENAME).write_text(decision.to_markdown())
    return out / DECISION_FILENAME


def promote(
    candidate_dir: str | Path,
    champion_dir: str | Path,
    registry_path: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Publica un candidato como campeon y lo anota en el historial.

    `force` permite promover un candidato cuya decision fue negativa. Es la via para
    hacer rollback: volver a promover una version vieja desde `releases/`.
    """
    candidate = Path(candidate_dir)
    champion = Path(champion_dir)

    decision = _read_json(candidate / DECISION_FILENAME)
    if decision is None:
        raise FileNotFoundError(
            f"No hay {DECISION_FILENAME} en {candidate}: correr `churn retrain`."
        )
    if not decision["promote"] and not force:
        raise ValueError(
            f"La decision para {decision.get('version') or candidate.name} fue no promover "
            f"({decision.get('reason')}). Usar --force para promoverlo igual."
        )

    version = decision.get("version") or candidate.name
    previous = _read_json(champion / RELEASE_FILENAME)

    # Copia permanente de la version: los candidatos se borran por ciclo de vida del
    # bucket, las releases no. Si la fuente ya es la release (rollback), no hay que copiar.
    release_dir = champion.parent / "releases" / version
    if release_dir.resolve() != candidate.resolve():
        _copy_artifact(candidate, release_dir)

    # metadata.json va al final: es lo que lee la API para /model, asi que nunca apunta
    # a un modelo que todavia no termino de copiarse.
    _copy_artifact(candidate, champion)

    release = {
        "version": version,
        "promoted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "previous_version": previous.get("version") if previous else None,
        "forced": bool(force or decision.get("forced", False)),
        "metrics": decision.get("candidate", {}),
    }
    _write_json(champion / RELEASE_FILENAME, release)

    registry = Path(registry_path) if registry_path else champion.parent / REGISTRY_FILENAME
    history = _read_json(registry) or {"champion": None, "history": []}
    history["champion"] = version
    history["history"].append(release)
    _write_json(registry, history)

    logger.info("Campeon: %s (antes %s)", version, release["previous_version"])
    return release


def _champion_metrics(
    features: pd.DataFrame, cfg: Config, champion_dir: str | Path | None
) -> tuple[dict[str, Any] | None, str | None, str]:
    if champion_dir is None or not (Path(champion_dir) / METADATA_FILENAME).exists():
        return None, None, SIN_CAMPEON

    champion = Path(champion_dir)
    release = _read_json(champion / RELEASE_FILENAME) or {}
    version = release.get("version")

    try:
        artifact = load_model(champion)
    except Exception as exc:  # un pickle de otra version del paquete falla de muchas formas
        logger.warning("No se pudo cargar el campeon (%s); se usan sus metricas guardadas", exc)
        artifact = None

    if artifact is not None:
        faltantes = [c for c in artifact.feature_names if c not in features.columns]
        if not faltantes:
            return evaluate_artifact(features, cfg, artifact).to_dict(), version, MISMO_TEST
        logger.warning(
            "Al campeon le faltan %s features con el codigo actual (p. ej. %s); se usan sus "
            "metricas guardadas",
            len(faltantes),
            faltantes[:3],
        )

    metadata = _read_json(champion / METADATA_FILENAME) or {}
    return metadata.get("metrics", {}).get("test"), version, METRICAS_GUARDADAS


def _copy_artifact(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in (MODEL_FILENAME, DECISION_FILENAME, DECISION_REPORT_FILENAME, METADATA_FILENAME):
        if (source / name).exists():
            shutil.copyfile(source / name, target / name)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path) as fh:
        return json.load(fh)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{float(value):.4f}"
