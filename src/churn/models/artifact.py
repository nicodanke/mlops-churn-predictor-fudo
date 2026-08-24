"""Persistencia del modelo entrenado y todo lo necesario para reproducir el scoring."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from churn import __version__

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path("models")
MODEL_FILENAME = "churn_model.joblib"
METADATA_FILENAME = "metadata.json"


@dataclass
class ModelArtifact:
    """Modelo + contrato de features + metricas. Es lo unico que necesita el scoring."""

    model: Any
    calibrator: Any | None
    feature_names: list[str]
    categorical_features: list[str]
    category_levels: dict[str, list[str]]
    metrics: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    trained_at: str = ""
    training_periods: list[int] = field(default_factory=list)
    churn_baseline: float = 0.0
    decision_threshold: float = 0.5
    excluded_seasonal_accounts: int = 0
    package_version: str = __version__

    def predict_proba(self, X: pd.DataFrame) -> Any:
        """Probabilidad de churn, calibrada si hay calibrador entrenado."""
        X = self.align(X)
        raw = self.model.predict_proba(X)[:, 1]
        if self.calibrator is not None:
            return self.calibrator.predict(raw)
        return raw

    def align(self, X: pd.DataFrame) -> pd.DataFrame:
        """Reordena y completa columnas para que coincidan con el contrato de entrenamiento.

        Si en produccion aparece una feature nueva se ignora, y si falta una se agrega
        vacia: XGBoost trata los NaN de forma nativa, asi que degrada en vez de romper.
        """
        missing = [c for c in self.feature_names if c not in X.columns]
        if missing:
            logger.warning("Faltan %s features en el input; se completan con NaN", len(missing))
            for col in missing:
                X[col] = pd.NA

        X = X[self.feature_names].copy()
        for col in self.categorical_features:
            levels = self.category_levels.get(col, [])
            X[col] = pd.Categorical(X[col].astype("object"), categories=levels)
        return X

    def to_metadata(self) -> dict[str, Any]:
        return {
            "package_version": self.package_version,
            "trained_at": self.trained_at,
            "n_features": len(self.feature_names),
            "feature_names": self.feature_names,
            "categorical_features": self.categorical_features,
            "training_periods": self.training_periods,
            "churn_baseline": self.churn_baseline,
            "decision_threshold": self.decision_threshold,
            "excluded_seasonal_accounts": self.excluded_seasonal_accounts,
            "metrics": self.metrics,
            "config": self.config,
        }


def save_model(artifact: ModelArtifact, model_dir: str | Path = DEFAULT_MODEL_DIR) -> Path:
    """Guarda el artefacto y un metadata.json legible al lado."""
    out_dir = Path(model_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    artifact.trained_at = artifact.trained_at or datetime.now(UTC).isoformat(timespec="seconds")

    model_path = out_dir / MODEL_FILENAME
    joblib.dump(artifact, model_path, compress=3)

    with open(out_dir / METADATA_FILENAME, "w") as fh:
        json.dump(artifact.to_metadata(), fh, indent=2, ensure_ascii=False)

    size_mb = model_path.stat().st_size / 1e6
    logger.info("Modelo guardado en %s (%.1f MB)", model_path, size_mb)
    return model_path


def load_model(model_dir: str | Path = DEFAULT_MODEL_DIR) -> ModelArtifact:
    path = Path(model_dir) / MODEL_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"No hay modelo entrenado en {path}. Corre `make train` primero.")
    artifact: ModelArtifact = joblib.load(path)
    logger.info(
        "Modelo cargado (entrenado %s, %s features)",
        artifact.trained_at,
        len(artifact.feature_names),
    )
    return artifact
