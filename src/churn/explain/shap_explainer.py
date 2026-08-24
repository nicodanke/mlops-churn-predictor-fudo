"""Atribucion por cuenta usando SHAP.

La importancia global (por ganancia) responde "que mira el modelo en general". Para el
equipo de CX eso no alcanza: necesitan saber por que *esta* cuenta esta en riesgo. SHAP
descompone cada prediccion individual en la contribucion de cada feature, con signo:
cuanto empujo hacia arriba o hacia abajo el riesgo de esa cuenta en particular.

Se acompana cada contribucion con el percentil de la cuenta en esa feature respecto del
resto de la base del mismo mes, que es lo que alimenta el mapa de calor del dashboard.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
import shap

from churn.features.naming import feature_group, humanize_feature
from churn.models.artifact import ModelArtifact

logger = logging.getLogger(__name__)


@dataclass
class FeatureContribution:
    """Aporte de una feature al riesgo de una cuenta puntual."""

    feature: str
    label: str
    group: str
    value: float | str | None
    shap_value: float
    direction: str  # "aumenta" | "reduce"
    percentile: float | None  # posicion de la cuenta en la base del mes, 0-100

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShapExplainer:
    """Envuelve el TreeExplainer de SHAP y traduce la salida a algo mostrable."""

    def __init__(self, artifact: ModelArtifact, top_n: int = 8):
        self.artifact = artifact
        self.top_n = top_n
        self._explainer = shap.TreeExplainer(artifact.model)

    def explain(self, X: pd.DataFrame) -> tuple[np.ndarray, list[list[FeatureContribution]]]:
        """Devuelve la matriz de SHAP values y el top-N de contribuciones por fila."""
        X = self.artifact.align(X)
        logger.info("Calculando SHAP para %s filas x %s features", f"{len(X):,}", X.shape[1])

        shap_values = self._explainer.shap_values(X, check_additivity=False)
        if isinstance(shap_values, list):  # binario en versiones viejas
            shap_values = shap_values[1]
        shap_values = np.asarray(shap_values, dtype="float32")

        percentiles = _percentile_table(X)
        contributions = self._top_contributions(X, shap_values, percentiles)
        return shap_values, contributions

    def _top_contributions(
        self, X: pd.DataFrame, shap_values: np.ndarray, percentiles: pd.DataFrame
    ) -> list[list[FeatureContribution]]:
        names = list(X.columns)
        # Indices de las top-N features por |shap| para cada fila.
        order = np.argsort(-np.abs(shap_values), axis=1)[:, : self.top_n]

        values = X.to_numpy(dtype="object")
        pct = percentiles.to_numpy(dtype="float32")

        out: list[list[FeatureContribution]] = []
        for row_i in range(len(X)):
            row: list[FeatureContribution] = []
            for col_i in order[row_i]:
                sv = float(shap_values[row_i, col_i])
                if sv == 0.0:
                    continue
                name = names[col_i]
                row.append(
                    FeatureContribution(
                        feature=name,
                        label=humanize_feature(name),
                        group=feature_group(name),
                        value=_clean(values[row_i, col_i]),
                        shap_value=round(sv, 5),
                        direction="aumenta" if sv > 0 else "reduce",
                        percentile=_clean_pct(pct[row_i, col_i]),
                    )
                )
            out.append(row)
        return out


def _percentile_table(X: pd.DataFrame) -> pd.DataFrame:
    """Percentil de cada valor dentro de su columna, sobre el lote scoreado."""
    numeric = X.select_dtypes(include=[np.number])
    ranked = numeric.rank(pct=True, na_option="keep") * 100
    return ranked.reindex(columns=X.columns).astype("float32")


def _clean(value: Any) -> float | str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, np.integer | np.floating):
        value = value.item()
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, int | str):
        return value
    return str(value)


def _clean_pct(value: float) -> float | None:
    return None if value is None or np.isnan(value) else round(float(value), 1)


def global_importance_from_shap(
    shap_values: np.ndarray, feature_names: list[str], top_n: int = 25
) -> pd.DataFrame:
    """Importancia global = |SHAP| promedio. Mas fiel que la ganancia del booster."""
    mean_abs = np.abs(shap_values).mean(axis=0)
    out = pd.DataFrame({"feature": feature_names, "mean_abs_shap": mean_abs})
    out["label"] = out["feature"].map(humanize_feature)
    out["group"] = out["feature"].map(feature_group)
    return out.sort_values("mean_abs_shap", ascending=False).head(top_n).reset_index(drop=True)
