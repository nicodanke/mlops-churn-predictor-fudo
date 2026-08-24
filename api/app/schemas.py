"""Contratos de entrada y salida de la API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RiskCategory = Literal["alto", "medio", "bajo", "no_churn"]


class FeatureContributionOut(BaseModel):
    feature: str = Field(description="Nombre tecnico de la feature.")
    label: str = Field(description="Nombre legible para mostrar en el dashboard.")
    group: str = Field(description="Dimension de negocio a la que pertenece.")
    value: float | str | None = Field(description="Valor de la cuenta en esa feature.")
    shap_value: float = Field(description="Aporte al riesgo. Positivo empuja hacia la baja.")
    direction: Literal["aumenta", "reduce"]
    percentile: float | None = Field(
        default=None, description="Posicion de la cuenta en la base del mes, 0-100."
    )


class AccountPrediction(BaseModel):
    id: int
    nombre: str | None = None
    plan: str | None = None
    plan_descripcion: str | None = None
    pais: str | None = None
    estado: str | None = None
    periodo: int = Field(description="Periodo del snapshot usado, YYYYMM.")
    periodo_prediccion: int = Field(description="Periodo sobre el que se predice la baja.")
    churn_probability: float
    monthly_revenue: float
    revenue_at_risk: float
    risk_category: RiskCategory
    will_churn: int
    diagnostico: str | None = None
    precio_incompleto: bool = False
    pausas_historicas: int = Field(
        default=0, description="Veces que la cuenta se ausento y volvio, antes de este mes."
    )
    mes_de_pausa_habitual: int | None = Field(
        default=None, description="Mes calendario en que la cuenta suele pausar, 1-12."
    )
    posible_estacional: bool = Field(
        default=False,
        description=(
            "La cuenta tiene un patron de pausas que sugiere un negocio de temporada. "
            "Su baja puede ser un cierre estacional y no una perdida."
        ),
    )


class AccountDetail(AccountPrediction):
    top_features: list[FeatureContributionOut] = []
    revenue_desglose: str | None = Field(
        default=None, description="Desglose del revenue por plan y modulo, serializado en JSON."
    )


class RiskSummaryRow(BaseModel):
    risk_category: RiskCategory
    cuentas: int
    prob_promedio: float
    revenue_mensual: float
    revenue_en_riesgo: float


class BatchSummary(BaseModel):
    periodo: int
    periodo_prediccion: int
    generated_at: str
    n_accounts: int
    currency: str
    pricing_loaded: bool
    decision_threshold: float
    risk_bands: dict[str, float]
    summary: list[RiskSummaryRow]


class PagedAccounts(BaseModel):
    total: int
    page: int
    size: int
    pages: int
    items: list[AccountPrediction]


class GlobalImportanceRow(BaseModel):
    feature: str
    label: str
    group: str
    mean_abs_shap: float


class ModelInfo(BaseModel):
    trained_at: str
    n_features: int
    training_periods: list[int]
    churn_baseline: float
    decision_threshold: float
    metrics: dict[str, Any]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    predictions_loaded: bool
    periodos_disponibles: list[int]
    model_loaded: bool
