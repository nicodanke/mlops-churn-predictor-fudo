"""Entrenamiento del clasificador de churn.

Modelo: XGBoost sobre gradient boosted trees. Es la eleccion adecuada para este dataset
tabular, mixto (numericas + categoricas), con muchos missings estructurales (los canales
de delivery que la cuenta no usa vienen vacios) y una clase positiva del ~4%: los arboles
manejan los NaN de forma nativa y no requieren escalado ni imputacion.

Sobre el desbalance se usa `scale_pos_weight` en lugar de resampleo, y como eso deforma
las probabilidades (que aca no son un detalle: el riesgo economico se calcula con ellas)
se calibra despues con regresion isotonica sobre el set de validacion.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from xgboost import XGBClassifier

from churn.config import Config
from churn.data.seasonality import SeasonalityConfig, seasonal_account_ids
from churn.features.builder import feature_columns
from churn.models.artifact import ModelArtifact
from churn.models.evaluate import evaluate_predictions
from churn.models.split import temporal_split

logger = logging.getLogger(__name__)

# Marcador para soltar los frames del split una vez extraidas las matrices.
EMPTY_FRAME = pd.DataFrame()


def train_model(features: pd.DataFrame, cfg: Config) -> tuple[ModelArtifact, dict[str, Any]]:
    """Entrena, calibra y evalua. Devuelve el artefacto listo para persistir."""
    excluidas: set[int] = set()
    if cfg.get("seasonality.exclude_from_training", False):
        excluidas = seasonal_account_ids(features, SeasonalityConfig.from_config(cfg))

    split = temporal_split(
        features,
        n_test_periods=int(cfg.get("split.n_test_periods", 2)),
        n_val_periods=int(cfg.get("split.n_val_periods", 2)),
        min_months_in_panel=int(cfg.get("labeling.min_months_in_panel", 2)),
        exclude_account_ids=excluidas,
    )

    feat_cols = feature_columns(features, cfg.get("features.exclude_patterns"))
    cat_cols = [c for c in feat_cols if isinstance(features[c].dtype, pd.CategoricalDtype)]
    category_levels = {c: [str(v) for v in features[c].cat.categories] for c in cat_cols}
    logger.info("Entrenando con %s features (%s categoricas)", len(feat_cols), len(cat_cols))

    X_train, y_train = _matrix(split.train, feat_cols, cat_cols, category_levels)
    X_val, y_val = _matrix(split.val, feat_cols, cat_cols, category_levels)
    X_test, y_test = _matrix(split.test, feat_cols, cat_cols, category_levels)

    # Los frames del split ya no se usan: solo se necesitaba el resumen para el reporte.
    resumen_split = split.summary()
    split.train = split.val = split.test = EMPTY_FRAME

    params = dict(cfg.get("model.params", {}))
    spw = cfg.get("model.scale_pos_weight")
    if spw is None:
        n_pos = int(y_train.sum())
        spw = float((len(y_train) - n_pos) / max(n_pos, 1))
        logger.info("scale_pos_weight calculado automaticamente: %.2f", spw)
    params["scale_pos_weight"] = spw
    params["enable_categorical"] = True
    params["early_stopping_rounds"] = int(cfg.get("model.early_stopping_rounds", 100))

    model = XGBClassifier(**params)
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    best_iter = getattr(model, "best_iteration", None)
    if best_iter is not None:
        logger.info("Early stopping en la iteracion %s", best_iter)

    calibrator = _fit_calibrator(model, X_val, y_val, cfg.get("model.calibration"))

    def predict(X: pd.DataFrame) -> np.ndarray:
        raw = model.predict_proba(X)[:, 1]
        return calibrator.predict(raw) if calibrator is not None else raw

    top_k = list(cfg.get("evaluation.top_k_fractions", [0.01, 0.05, 0.10, 0.20]))
    results = {
        "val": evaluate_predictions(y_val, predict(X_val), top_k),
        "test": evaluate_predictions(y_test, predict(X_test), top_k),
    }

    threshold = cfg.get("risk.no_churn_threshold")
    if threshold is None:
        threshold = results["val"].best_threshold
        logger.info("Umbral de decision calibrado sobre validacion: %.4f", threshold)

    artifact = ModelArtifact(
        model=model,
        calibrator=calibrator,
        feature_names=feat_cols,
        categorical_features=cat_cols,
        category_levels=category_levels,
        metrics={name: res.to_dict() for name, res in results.items()},
        config=cfg.raw,
        trained_at=datetime.now(UTC).isoformat(timespec="seconds"),
        training_periods=[int(p) for p in split.train_periods],
        excluded_seasonal_accounts=len(excluidas),
        churn_baseline=float(y_train.mean()),
        decision_threshold=float(threshold),
    )

    report = {
        "split_summary": resumen_split,
        "results": results,
        "feature_importance": feature_importance(model, feat_cols),
        "test_periods": split.test_periods,
        "val_periods": split.val_periods,
    }
    return artifact, report


def _matrix(
    df: pd.DataFrame,
    feat_cols: list[str],
    cat_cols: list[str],
    levels: dict[str, list[str]],
) -> tuple[pd.DataFrame, np.ndarray]:
    # Sin `.copy()`: duplicaba cientos de megabytes por cada uno de los tres splits.
    # Las categoricas ya vienen tipadas del feature builder con los mismos niveles que
    # se guardan en el artefacto, asi que normalmente no hay nada que recastear; se
    # comprueba igual por si alguna quedo con otro conjunto de categorias.
    X = df[feat_cols]
    desalineadas = {
        col: pd.Categorical(X[col].astype("object"), categories=levels[col])
        for col in cat_cols
        if list(X[col].cat.categories) != levels[col]
    }
    if desalineadas:
        X = X.assign(**desalineadas)
    return X, df["churn"].to_numpy().astype(int)


def _fit_calibrator(
    model: XGBClassifier, X_val: pd.DataFrame, y_val: np.ndarray, method: str | None
) -> IsotonicRegression | None:
    """Reajusta las probabilidades sobre validacion.

    Sin esto, `scale_pos_weight` deja probabilidades infladas (un 0.6 que en realidad
    significa 0.15). Como el riesgo economico es probabilidad x revenue, una probabilidad
    mal calibrada se traduce directo en plata mal priorizada.
    """
    if not method:
        return None
    if method != "isotonic":
        logger.warning("Calibracion '%s' no implementada; se usa isotonic", method)

    raw = model.predict_proba(X_val)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    calibrator.fit(raw, y_val)

    logger.info(
        "Calibracion isotonica: media predicha %.4f -> %.4f (real %.4f)",
        float(raw.mean()),
        float(calibrator.predict(raw).mean()),
        float(y_val.mean()),
    )
    return calibrator


def feature_importance(model: XGBClassifier, feat_cols: list[str], top_n: int = 30) -> pd.DataFrame:
    """Importancia global por ganancia. Es la vista de 'que mira el modelo' en agregado."""
    booster = model.get_booster()
    gain = booster.get_score(importance_type="gain")
    # XGBoost devuelve las claves como f0, f1... o con el nombre segun la version.
    rows = []
    for key, value in gain.items():
        name = feat_cols[int(key[1:])] if key.startswith("f") and key[1:].isdigit() else key
        rows.append({"feature": name, "gain": value})
    out = pd.DataFrame(rows).sort_values("gain", ascending=False).head(top_n)
    out["gain_pct"] = (out["gain"] / out["gain"].sum() * 100).round(2)
    return out.reset_index(drop=True)
