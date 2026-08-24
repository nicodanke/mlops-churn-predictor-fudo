"""Orquestacion de las etapas del pipeline.

Cada etapa es independiente y cachea su salida en parquet, de modo que iterar sobre el
modelo no obligue a releer y reprocesar el CSV completo cada vez.

    prepare  CSV crudo   -> panel etiquetado + features   (outputs/interim/features.parquet)
    train    features    -> modelo + metricas             (models/)
    score    features    -> predicciones del ultimo mes   (outputs/predictions/)
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from churn.config import Config
from churn.data.labeling import build_labels, churn_rate_by_period
from churn.data.loader import load_panel, resolve_sources
from churn.features.builder import build_features
from churn.models.artifact import save_model
from churn.models.train import train_model

logger = logging.getLogger(__name__)


def prepare(cfg: Config, force: bool = False) -> pd.DataFrame:
    """Lee el snapshot, infiere las etiquetas de churn y construye las features."""
    features_path = Path(cfg.path_of("data.features_path"))
    raw_path = cfg.path_of("data.raw_path")

    if features_path.exists() and not force:
        if _cache_is_stale(raw_path, features_path):
            logger.info(
                "El snapshot es mas nuevo que las features cacheadas: se recalculan. "
                "Es lo que pasa cuando se agrega el archivo de un mes nuevo a data/."
            )
        else:
            logger.info(
                "Reusando features cacheadas en %s (usa --force para recalcular)", features_path
            )
            return pd.read_parquet(features_path)

    panel = load_panel(raw_path)

    labeled = build_labels(
        panel,
        confirm_window=int(cfg.get("labeling.confirm_window", 3)),
        ambiguous_policy=str(cfg.get("labeling.ambiguous_policy", "drop")),
    )

    features = build_features(
        labeled,
        lags=list(cfg.get("features.lags", [1, 2, 3])),
        rolling_window=int(cfg.get("features.rolling_window", 3)),
    )

    features_path.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(features_path, index=False)
    logger.info("Features guardadas en %s", features_path)

    interim_path = Path(cfg.path_of("data.interim_path"))
    interim_path.parent.mkdir(parents=True, exist_ok=True)
    labeled_cols = [
        "id",
        "periodo",
        "t",
        "plan",
        "pais",
        "estado",
        "nombre",
        "churn",
        "is_ambiguous",
        "is_labelable",
        "months_in_panel",
    ]
    labeled[[c for c in labeled_cols if c in labeled.columns]].to_parquet(interim_path, index=False)

    return features


def train(cfg: Config, features: pd.DataFrame | None = None, model_dir: str | Path = "models"):
    """Entrena el modelo y persiste el artefacto."""
    features = features if features is not None else prepare(cfg)
    artifact, report = train_model(features, cfg)
    save_model(artifact, model_dir)
    return artifact, report


def churn_baseline_table(cfg: Config, features: pd.DataFrame | None = None) -> pd.DataFrame:
    """Churn rate observado por periodo: el numero contra el que se compara el modelo."""
    features = features if features is not None else prepare(cfg)
    return churn_rate_by_period(features)


def _cache_is_stale(raw_path: str, features_path: Path) -> bool:
    """True si algun archivo del snapshot es mas nuevo que las features cacheadas.

    Sin esto, agregar el mes nuevo a data/ y correr `churn score` seguia sirviendo el
    panel viejo, sin error y sin aviso: las predicciones simplemente quedaban un mes
    atrasadas. Es la clase de problema que no se nota hasta que alguien compara numeros.

    Las rutas remotas (gs://) no se comprueban: se asume que el job mensual las regenera
    de punta a punta con --force.
    """
    if "://" in raw_path:
        return False

    try:
        fuentes = resolve_sources(raw_path)
        mas_nuevo = max(Path(f).stat().st_mtime for f in fuentes)
        return mas_nuevo > features_path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return False
