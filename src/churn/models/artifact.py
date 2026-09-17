"""Persistencia del modelo entrenado y todo lo necesario para reproducir el scoring."""

from __future__ import annotations

import io
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


def save_model(artifact: ModelArtifact, model_dir: str | Path = DEFAULT_MODEL_DIR) -> str:
    """Guarda el artefacto y un metadata.json legible al lado.

    `model_dir` puede ser un directorio local o un bucket (`gs://bucket/models/...`), que
    es donde queda el modelo cuando se entrena desde Cloud Shell (ver deploy/cloudshell.sh).
    """
    out_dir = _join(model_dir)
    if not _is_remote(out_dir):
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    artifact.trained_at = artifact.trained_at or datetime.now(UTC).isoformat(timespec="seconds")

    # Se serializa en memoria para escribir igual en disco que en el bucket. El artefacto
    # pesa unos pocos MB.
    buffer = io.BytesIO()
    joblib.dump(artifact, buffer, compress=3)
    model_path = _join(out_dir, MODEL_FILENAME)
    with _open(model_path, "wb") as fh:
        fh.write(buffer.getbuffer())

    # metadata.json va despues del modelo: nunca describe un modelo que no termino de subir.
    with _open(_join(out_dir, METADATA_FILENAME), "w") as fh:
        json.dump(artifact.to_metadata(), fh, indent=2, ensure_ascii=False)

    logger.info("Modelo guardado en %s (%.1f MB)", model_path, buffer.tell() / 1e6)
    return model_path


def load_model(model_dir: str | Path = DEFAULT_MODEL_DIR) -> ModelArtifact:
    path = _join(model_dir, MODEL_FILENAME)
    if not _exists(path):
        raise FileNotFoundError(f"No hay modelo entrenado en {path}. Corre `make train` primero.")
    with _open(path, "rb") as fh:
        artifact: ModelArtifact = joblib.load(io.BytesIO(fh.read()))
    logger.info(
        "Modelo cargado (entrenado %s, %s features)",
        artifact.trained_at,
        len(artifact.feature_names),
    )
    return artifact


def _is_remote(path: str | Path) -> bool:
    return "://" in str(path)


def _join(base: str | Path, *parts: str) -> str:
    """Une rutas sin romper el `gs://` del bucket (Path lo colapsa a `gs:/`)."""
    if _is_remote(base):
        return "/".join([str(base).rstrip("/"), *parts])
    return str(Path(base, *parts))


def _open(path: str, mode: str) -> Any:
    """Abre un archivo local o de un bucket. Los buckets necesitan el extra `gcp` (gcsfs)."""
    kwargs = {} if "b" in mode else {"encoding": "utf-8"}
    if not _is_remote(path):
        return open(path, mode, **kwargs)
    import fsspec

    return fsspec.open(path, mode, **kwargs)


def _exists(path: str) -> bool:
    if not _is_remote(path):
        return Path(path).exists()
    import fsspec

    fs, ruta = fsspec.core.url_to_fs(path)
    return fs.exists(ruta)
