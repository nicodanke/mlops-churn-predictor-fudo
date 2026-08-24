"""De probabilidad de churn a categoria de riesgo.

El modelo entrega una probabilidad. La categoria que consume el equipo de CX combina esa
probabilidad con lo que la cuenta factura, porque no es lo mismo perder una cuenta de
20 USD/mes que una de 200 con la misma probabilidad:

    revenue_en_riesgo = probabilidad_de_churn * revenue_mensual

  no_churn  probabilidad por debajo del umbral de decision
  bajo      por encima del umbral, pero con poco revenue en juego
  medio     revenue en riesgo por encima del corte medio
  alto      revenue en riesgo por encima del corte alto

Los cortes viven en `config/model.yaml` y se definen con negocio, no aca.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

RISK_ORDER = ["no_churn", "bajo", "medio", "alto"]


@dataclass
class RiskBands:
    """Umbrales que traducen probabilidad + revenue en una categoria."""

    no_churn_threshold: float
    alto: float
    medio: float

    @classmethod
    def from_config(cls, cfg, fallback_threshold: float = 0.5) -> RiskBands:
        bands = cfg.get("risk.revenue_at_risk_bands", {}) or {}
        threshold = cfg.get("risk.no_churn_threshold")
        return cls(
            no_churn_threshold=float(threshold if threshold is not None else fallback_threshold),
            alto=float(bands.get("alto", 40.0)),
            medio=float(bands.get("medio", 15.0)),
        )


def assign_risk(
    probability: pd.Series | np.ndarray,
    revenue: pd.Series | np.ndarray,
    bands: RiskBands,
) -> pd.DataFrame:
    """Devuelve revenue en riesgo y categoria para cada cuenta."""
    prob = pd.Series(np.asarray(probability, dtype="float64")).reset_index(drop=True)
    rev = pd.Series(np.asarray(revenue, dtype="float64")).reset_index(drop=True).fillna(0.0)

    at_risk = prob * rev

    category = pd.Series("no_churn", index=prob.index, dtype="object")
    flagged = prob >= bands.no_churn_threshold

    category[flagged] = "bajo"
    category[flagged & (at_risk >= bands.medio)] = "medio"
    category[flagged & (at_risk >= bands.alto)] = "alto"

    return pd.DataFrame(
        {
            "churn_probability": prob.round(6),
            "monthly_revenue": rev.round(2),
            "revenue_at_risk": at_risk.round(2),
            "risk_category": pd.Categorical(category, categories=RISK_ORDER, ordered=True),
            "will_churn": flagged.astype(int),
        }
    )


def risk_summary(scored: pd.DataFrame) -> pd.DataFrame:
    """Cuentas y revenue en riesgo por categoria: la vista de cabecera del dashboard."""
    grouped = scored.groupby("risk_category", observed=False).agg(
        cuentas=("churn_probability", "size"),
        prob_promedio=("churn_probability", "mean"),
        revenue_mensual=("monthly_revenue", "sum"),
        revenue_en_riesgo=("revenue_at_risk", "sum"),
    )
    grouped["prob_promedio"] = (grouped["prob_promedio"] * 100).round(1)
    grouped["revenue_mensual"] = grouped["revenue_mensual"].round(0)
    grouped["revenue_en_riesgo"] = grouped["revenue_en_riesgo"].round(0)

    # Una categoria sin cuentas deja el promedio en NaN, y NaN no es JSON valido: el
    # endpoint de resumen respondia 500 y el dashboard no cargaba. Pasa de verdad —
    # con la lista de precios sin completar, "alto" y "medio" quedan las dos en cero.
    return grouped.fillna(0).reset_index()
