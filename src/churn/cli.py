"""Interfaz de linea de comandos del pipeline.

churn prepare              lee el snapshot, etiqueta el churn y construye features
churn train                entrena el modelo y reporta las metricas offline
churn score                corre el scoring batch del ultimo periodo
churn pricing-template     genera / actualiza config/pricing.yaml
churn pricing-check        muestra cuanto falta completar de la lista de precios
churn baseline             churn rate observado por periodo
churn run-all              prepare + train + score, de punta a punta
churn retrain              entrena un candidato y decide si le gana al modelo en produccion
churn promote              publica un candidato como campeon (o hace rollback)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from churn import pipeline
from churn.config import DEFAULT_PRICING_PATH, Config
from churn.data.intercom import attribute_accounts, attribution_report, load_companies, load_tickets
from churn.data.seasonality import (
    NOMBRE_MES,
    SeasonalityConfig,
    account_seasonality,
    monthly_profile,
    seasonality_by_country,
)
from churn.logging_setup import setup_logging
from churn.models import promotion
from churn.models.artifact import load_model
from churn.pricing.revenue import PricingBook
from churn.pricing.template import build_pricing_template, pricing_coverage
from churn.scoring.batch import score_period, write_predictions
from churn.scoring.risk import risk_summary

app = typer.Typer(add_completion=False, help="Predictor de churn de cuentas Fudo.")
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Ruta al YAML de configuracion.")
ModelDirOpt = typer.Option("models", "--model-dir", help="Directorio del artefacto del modelo.")


@app.callback()
def main(log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARNING")) -> None:
    setup_logging(log_level)


@app.command()
def prepare(
    config: str | None = ConfigOpt,
    force: bool = typer.Option(False, "--force", help="Recalcular aunque exista el cache."),
) -> None:
    """Construye el panel etiquetado y la matriz de features."""
    cfg = Config.load(config)
    features = pipeline.prepare(cfg, force=force)
    console.print(
        f"[green]Listo[/green]: {len(features):,} filas, "
        f"{features['id'].nunique():,} cuentas, "
        f"periodos {features['periodo'].min()} a {features['periodo'].max()}"
    )


@app.command()
def train(config: str | None = ConfigOpt, model_dir: str = ModelDirOpt) -> None:
    """Entrena el modelo de churn y reporta las metricas de validacion y test."""
    cfg = Config.load(config)
    artifact, report = pipeline.train(cfg, model_dir=model_dir)

    console.print("\n[bold]Split temporal[/bold]")
    _print_df(report["split_summary"])

    min_pr_auc = float(cfg.get("evaluation.min_pr_auc", 0.20))
    for name in ("val", "test"):
        result = report["results"][name]
        periods = report[f"{name}_periods"]
        console.print(f"\n[bold]{name.upper()}[/bold]  periodos {periods[0]}-{periods[-1]}")
        console.print(result.render())

    test_pr_auc = report["results"]["test"].pr_auc
    if test_pr_auc >= min_pr_auc:
        console.print(
            f"\n[bold green]PR-AUC {test_pr_auc:.4f} >= umbral acordado con negocio "
            f"({min_pr_auc:.2f}). El modelo pasa el criterio de salida a produccion.[/bold green]"
        )
    else:
        console.print(
            f"\n[bold red]PR-AUC {test_pr_auc:.4f} por debajo del umbral {min_pr_auc:.2f}."
            f"[/bold red]"
        )

    console.print("\n[bold]Features mas importantes (ganancia)[/bold]")
    _print_df(report["feature_importance"].head(15))


@app.command()
def retrain(
    config: str | None = ConfigOpt,
    candidate_dir: str = typer.Option(
        ..., "--candidate-dir", help="Donde guardar el modelo candidato."
    ),
    champion_dir: str | None = typer.Option(
        None, "--champion-dir", help="Modelo en produccion contra el que se compara."
    ),
    version: str = typer.Option("", "--version", help="Identificador del candidato (SHA)."),
    force: bool = typer.Option(
        True, "--force/--use-cache", help="Recalcular las features o reusar el cache."
    ),
    force_promote: bool = typer.Option(
        False,
        "--force-promote",
        help="Promover aunque no mejore al campeon. El umbral de negocio se exige igual.",
    ),
) -> None:
    """Entrena un candidato y decide si reemplaza al modelo en produccion.

    Es el paso que corre CI en cada cambio del modelo. Deja el candidato y su
    decision.json en --candidate-dir; la promocion es un paso aparte (`churn promote`).
    """
    cfg = Config.load(config)
    features = pipeline.prepare(cfg, force=force)
    artifact, _ = pipeline.train(cfg, features, model_dir=candidate_dir)

    decision = promotion.challenge(
        features, cfg, artifact, champion_dir, version=version, force=force_promote
    )
    path = promotion.write_decision(decision, candidate_dir)
    console.print(Markdown(decision.to_markdown()))
    console.print(f"  decision -> {path}")


@app.command()
def promote(
    candidate_dir: str = typer.Option(..., "--candidate-dir", help="Modelo a publicar."),
    champion_dir: str = typer.Option(..., "--champion-dir", help="Directorio del campeon."),
    registry: str | None = typer.Option(
        None, "--registry", help="Historial de versiones. Por defecto registry.json al lado."
    ),
    force: bool = typer.Option(
        False, "--force", help="Promover aunque la decision haya sido negativa (rollback)."
    ),
) -> None:
    """Publica un candidato como campeon y lo anota en el historial de versiones."""
    try:
        release = promotion.promote(candidate_dir, champion_dir, registry, force=force)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Campeon[/green]: {release['version']} "
        f"(antes {release['previous_version'] or 'ninguno'})"
    )


@app.command()
def score(
    config: str | None = ConfigOpt,
    model_dir: str = ModelDirOpt,
    periodo: int | None = typer.Option(
        None, help="Periodo YYYYMM a scorear. Por defecto el ultimo."
    ),
    narrative: str = typer.Option("rules", help="Generador de texto: rules | llm."),
    pricing: str | None = typer.Option(
        None, "--pricing", help="Lista de precios a usar. Por defecto config/pricing.yaml."
    ),
) -> None:
    """Corre el scoring batch y escribe las predicciones a disco."""
    cfg = Config.load(config)
    features = pipeline.prepare(cfg)
    artifact = load_model(model_dir)

    scored, metadata = score_period(
        features,
        artifact,
        cfg,
        periodo=periodo,
        narrative_mode=narrative,
        pricing=PricingBook.load(pricing) if pricing else None,
    )
    written = write_predictions(
        scored,
        metadata,
        cfg.path_of("scoring.output_dir"),
        list(cfg.get("scoring.formats", ["parquet", "json"])),
    )

    console.print(
        f"\n[bold]Scoring del periodo {metadata['periodo_snapshot']}[/bold] "
        f"(prediccion de baja en {metadata['periodo_prediccion']})"
    )
    _print_df(risk_summary(scored))

    console.print("\n[bold]Top 10 cuentas por revenue en riesgo[/bold]")
    top = scored.head(10)[
        [
            "id",
            "nombre",
            "plan",
            "churn_probability",
            "monthly_revenue",
            "revenue_at_risk",
            "risk_category",
        ]
    ]
    _print_df(top)

    for kind, path in written.items():
        console.print(f"  {kind:9s} -> {path}")


@app.command(name="pricing-template")
def pricing_template(
    config: str | None = ConfigOpt,
    output: str = typer.Option(str(DEFAULT_PRICING_PATH), "--output", "-o"),
    currency: str = typer.Option("USD", help="Moneda de la lista de precios."),
) -> None:
    """Genera o actualiza config/pricing.yaml con todos los planes y modulos observados."""
    cfg = Config.load(config)
    features_path = Path(cfg.path_of("data.features_path"))

    if features_path.exists():
        plans = pd.read_parquet(features_path, columns=["plan"])["plan"]
    else:
        from churn.data.loader import load_raw

        plans = load_raw(cfg.path_of("data.raw_path"))["plan"]

    path = build_pricing_template(plans, output, currency=currency)
    console.print(
        f"[green]Plantilla escrita en {path}[/green]\n"
        "Completa los precios a mano y volve a correr `churn score`."
    )


@app.command(name="pricing-check")
def pricing_check(config: str | None = ConfigOpt, top: int = 25) -> None:
    """Muestra que planes todavia no tienen precio cargado y cuantas cuentas afectan."""
    cfg = Config.load(config)
    book = PricingBook.load()
    plans = pd.read_parquet(cfg.path_of("data.features_path"), columns=["periodo", "plan"])
    plans = plans[plans["periodo"] == plans["periodo"].max()]["plan"]

    coverage = pricing_coverage(plans, book)
    total = int(coverage["cuentas"].sum())
    complete = int(coverage.loc[coverage["completo"], "cuentas"].sum())

    console.print(
        f"Cobertura de precios sobre el ultimo periodo: "
        f"[bold]{complete:,}/{total:,}[/bold] cuentas ({complete / total:.1%})"
    )
    pending = coverage[~coverage["completo"]].head(top)
    if pending.empty:
        console.print("[green]Todos los planes tienen precio cargado.[/green]")
    else:
        console.print(f"\n[bold]Planes sin precio (top {top} por cantidad de cuentas)[/bold]")
        _print_df(pending[["plan", "cuentas", "faltantes"]])


@app.command()
def baseline(config: str | None = ConfigOpt) -> None:
    """Churn rate observado por periodo: el numero contra el que se compara el modelo."""
    cfg = Config.load(config)
    _print_df(pipeline.churn_baseline_table(cfg))


@app.command(name="run-all")
def run_all(
    config: str | None = ConfigOpt,
    model_dir: str = ModelDirOpt,
    force: bool = typer.Option(False, "--force"),
    pricing: str | None = typer.Option(None, "--pricing"),
) -> None:
    """Pipeline completo: prepare -> train -> score."""
    cfg = Config.load(config)
    features = pipeline.prepare(cfg, force=force)
    artifact, report = pipeline.train(cfg, features, model_dir=model_dir)
    console.print(report["results"]["test"].render())

    scored, metadata = score_period(
        features,
        artifact,
        cfg,
        pricing=PricingBook.load(pricing) if pricing else None,
    )
    write_predictions(
        scored,
        metadata,
        cfg.path_of("scoring.output_dir"),
        list(cfg.get("scoring.formats", ["parquet", "json"])),
    )
    _print_df(risk_summary(scored))


@app.command()
def info(model_dir: str = ModelDirOpt) -> None:
    """Muestra los metadatos del modelo entrenado."""
    artifact = load_model(model_dir)
    console.print_json(
        json.dumps(
            {
                "trained_at": artifact.trained_at,
                "n_features": len(artifact.feature_names),
                "training_periods": [artifact.training_periods[0], artifact.training_periods[-1]],
                "churn_baseline": round(artifact.churn_baseline, 4),
                "decision_threshold": round(artifact.decision_threshold, 4),
                "metrics": {
                    k: {m: v[m] for m in ("pr_auc", "roc_auc", "best_f1")}
                    for k, v in artifact.metrics.items()
                },
            }
        )
    )


@app.command()
def seasonality(
    config: str | None = ConfigOpt,
    detalle: int = typer.Option(15, "--detalle", help="Cuentas estacionales a listar."),
) -> None:
    """Analiza las cuentas con estacionalidad de uso y el ciclo anual de altas y bajas."""
    cfg = Config.load(config)
    panel = pipeline.prepare(cfg)
    seasonal_cfg = SeasonalityConfig.from_config(cfg)

    perfil = account_seasonality(panel, seasonal_cfg)
    estacionales = perfil[perfil["es_estacional"]] if not perfil.empty else perfil
    total_cuentas = panel["id"].nunique()

    console.print("\n[bold]Cuentas con pausas[/bold]")
    console.print(
        f"  {len(perfil):,} de {total_cuentas:,} cuentas ({len(perfil) / total_cuentas:.1%}) "
        f"se ausentaron y volvieron al menos una vez."
    )
    console.print(
        f"  {len(estacionales):,} quedan marcadas como estacionales "
        f"({len(estacionales) / total_cuentas:.1%} de la base)."
    )
    if len(estacionales):
        por_tipo = estacionales["tipo"].value_counts()
        console.print(
            f"  [bold]{int(por_tipo.get('temporada', 0)):,} de temporada[/bold] "
            "(cierran media temporada y reabren: el parador de playa) y "
            f"[bold]{int(por_tipo.get('intermitente', 0)):,} intermitentes[/bold] "
            "(pausas cortas y repetidas, sin patron de calendario)."
        )

    console.print("\n[bold]Ciclo anual: en que meses se dan de baja y de alta[/bold]")
    console.print(
        "  El indice compara contra un año uniforme: 1.00 es lo esperable, "
        "1.30 es 30% mas de lo normal en ese mes."
    )
    perfil_mensual = monthly_profile(panel)
    for evento in perfil_mensual["evento"].unique():
        bloque = perfil_mensual[perfil_mensual["evento"] == evento]
        destacados = bloque.nlargest(3, "indice")
        console.print(
            f"\n  [bold]{evento}[/bold] (n={int(bloque['n'].sum()):,}) — picos en "
            + ", ".join(f"{r.mes} ({r.indice:.2f})" for r in destacados.itertuples())
        )
        _print_df(bloque[["mes", "n", "pct", "indice"]])

    console.print("\n[bold]Por pais[/bold]")
    console.print(
        "  Si la estacionalidad fuera uniforme, 33% de las pausas caerian en abril-julio."
    )
    _print_df(seasonality_by_country(panel))

    if len(estacionales):
        console.print(f"\n[bold]Top {detalle} cuentas estacionales (por cantidad de pausas)[/bold]")
        nombres = panel.groupby("id")["nombre"].last()
        paises = panel.groupby("id")["pais"].last()
        # Primero las de temporada: son las que mas le importan a negocio.
        orden = estacionales.assign(_p=(estacionales["tipo"] == "temporada").astype(int))
        top = orden.nlargest(detalle, ["_p", "n_pausas", "duracion_total"]).copy()
        top["nombre"] = top["id"].map(nombres)
        top["pais"] = top["id"].map(paises)
        top["mes_baja"] = top["mes_baja_modal"].map(lambda m: NOMBRE_MES[int(m) - 1])
        top["mes_alta"] = top["mes_alta_modal"].map(lambda m: NOMBRE_MES[int(m) - 1])
        columnas = [
            "id",
            "nombre",
            "pais",
            "tipo",
            "n_pausas",
            "duracion_media",
            "mes_baja",
            "mes_alta",
        ]
        _print_df(top[columnas])

    if cfg.get("seasonality.exclude_from_training", False):
        console.print(
            f"\n[dim]Estas {len(estacionales):,} cuentas se excluyen del entrenamiento "
            f"(seasonality.exclude_from_training). Se siguen scoreando igual, marcadas "
            f"con su historial de pausas.[/dim]"
        )
    else:
        console.print(
            f"\n[dim]La exclusion del entrenamiento esta desactivada: se midio el A/B y "
            f"no aporta (ver config/model.yaml). Estas {len(estacionales):,} cuentas "
            f"entran al entrenamiento igual, y sus meses de pausa ya los descarta la "
            f"ventana de confirmacion del etiquetado.[/dim]"
        )


@app.command(name="intercom-check")
def intercom_check(
    tickets: str = typer.Option("data/intercom/intercom_data.csv", help="Export de tickets."),
    companies: str | None = typer.Option(
        None, "--companies", help="Export de companies de Intercom, para resolver el id de Fudo."
    ),
    config: str | None = ConfigOpt,
) -> None:
    """Evalua si un export de Intercom alcanza para usarlo como fuente de features.

    Reporta cuantos tickets se pueden atribuir a una cuenta, por que via, y cuantos caen
    en periodos que tienen etiqueta de churn — que es lo que decide si sirve para entrenar.
    """
    cfg = Config.load(config)
    tk = load_tickets(tickets)

    panel = pd.read_parquet(cfg.path_of("data.interim_path"), columns=["id", "periodo", "churn"])
    cuentas_validas = set(panel["id"].astype(int))

    comp = None
    if companies:
        comp = load_companies(companies)
        console.print(f"[green]Companies leidas:[/green] {len(comp):,} con id de Fudo")
        cruzan = comp["account_id"].isin(cuentas_validas).sum()
        console.print(
            f"  de esas, {cruzan:,} ({cruzan / max(len(comp), 1):.0%}) existen en el snapshot"
        )
        if cruzan / max(len(comp), 1) < 0.5:
            console.print(
                "[yellow]  Menos de la mitad cruza con el snapshot: puede que la columna "
                "detectada no sea el id de Fudo. Revisar el mapeo.[/yellow]"
            )

    tk = attribute_accounts(tk, comp, valid_account_ids=cuentas_validas)

    console.print("\n[bold]De donde sale la cuenta de cada ticket[/bold]")
    _print_df(attribution_report(tk))

    etiquetables = panel.loc[panel["churn"].notna(), "periodo"]
    lo, hi = int(etiquetables.min()), int(etiquetables.max())
    utiles = tk[tk["account_id"].notna() & tk["periodo"].between(lo, hi)]

    console.print(
        f"\n[bold]Utilidad para entrenar[/bold]\n"
        f"  periodos con etiqueta de churn: {lo} .. {hi}\n"
        f"  periodos cubiertos por tickets: {tk['periodo'].min()} .. {tk['periodo'].max()}\n"
        f"  tickets atribuidos que caen ahi dentro: [bold]{len(utiles):,}[/bold]"
        f" sobre {len(tk):,}\n"
        f"  cuentas alcanzadas: {utiles['account_id'].nunique():,}"
    )

    if len(utiles) < 5000:
        console.print(
            "\n[yellow]No alcanza para entrenar.[/yellow] Hacen falta tickets atribuidos "
            "dentro de los periodos etiquetados, y en volumen: con unos pocos cientos no se "
            "puede distinguir señal de ruido. Ver data/README.md para que pedir en el "
            "re-export."
        )
    else:
        console.print(
            "\n[green]Alcanza para intentarlo.[/green] Siguiente paso: construir las "
            "features de soporte con lag y medir si mejoran el PR-AUC."
        )


def _print_df(df: pd.DataFrame) -> None:
    table = Table(show_header=True, header_style="bold")
    for col in df.columns:
        table.add_column(str(col))
    for _, row in df.iterrows():
        table.add_row(*[_fmt(v) for v in row])
    console.print(table)


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}" if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


if __name__ == "__main__":
    app()
