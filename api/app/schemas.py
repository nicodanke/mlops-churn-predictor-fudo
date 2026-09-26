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
    eda_loaded: bool = False


# --------------------------------------------------------------------- EDA ---
# El reporte descriptivo que escribe `churn eda`. Las secciones se tipan a nivel de
# encabezado y sus tablas quedan como filas sueltas: son listas largas y heterogeneas
# que cambian cuando se agrega una funcionalidad al catalogo, y fijarlas aca obligaria
# a tocar dos archivos por cada cambio del reporte.


class EdaPanel(BaseModel):
    filas: int
    cuentas: int
    periodos: int
    periodo_desde: int
    periodo_hasta: int
    periodo_referencia: int


class EdaBase(BaseModel):
    """Salud de la base: cuanto crece y a que ritmo se va la gente."""

    serie: list[dict[str, Any]]
    cuentas_activas_ultimo: int
    cuentas_activas_primero: int
    crecimiento_total: float | None = None
    crecimiento_mensual: float
    altas_totales: int
    bajas_confirmadas_totales: int
    churn_rate_promedio: float
    churn_rate_min: float | None = None
    churn_rate_max: float | None = None
    filas_etiquetadas: int
    filas_ambiguas: int
    retencion_anual: float | None = None
    vida_media_meses: float | None = None
    periodos_sin_etiqueta: list[int] = []
    cuentas_con_pausas: int = 0
    cuentas_estacionales: int = 0
    pct_estacionales: float = 0.0
    paises: list[dict[str, Any]] = []


class EdaAdopcion(BaseModel):
    """Que parte del producto usa una cuenta, sobre el ultimo periodo del panel."""

    periodo: int
    cuentas: int
    grupos: list[dict[str, Any]]
    volumen: list[dict[str, Any]]


class EdaChurn(BaseModel):
    """En que se diferencia una cuenta que se va de una que se queda."""

    n_se_queda: int
    n_churn: int
    churn_rate: float | None = None
    senales: list[dict[str, Any]]
    estado_cobranza: list[dict[str, Any]] = []
    antiguedad: list[dict[str, Any]] = []


class EdaReport(BaseModel):
    generated_at: str
    panel: EdaPanel
    base: EdaBase
    adopcion: EdaAdopcion
    churn: EdaChurn
