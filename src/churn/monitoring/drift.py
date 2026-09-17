"""Drift: cuanto se corrieron los datos desde que se entreno el modelo.

El modelo se entrena una vez y despues scorea meses que nunca vio. La pregunta de
monitoreo es si el mundo que modelo sigue siendo el mismo: si la distribucion de las
features se movio, las probabilidades dejan de ser confiables aunque el pipeline no tire
ningun error. Es lo que pasa cuando cambia el mix de planes, cuando entra un pais nuevo o
cuando el reporte del data warehouse empieza a traer una columna vacia.

Se mide con el Population Stability Index, que compara la misma variable en dos momentos:

    PSI = suma( (actual_i - referencia_i) * ln(actual_i / referencia_i) )

sobre tramos definidos por los cuantiles de la referencia. La lectura habitual, que es la
que usa el reporte:

    PSI < 0.10     sin cambios relevantes
    0.10 a 0.25    cambio moderado, mirarlo
    PSI >= 0.25    cambio grande, revisar antes de confiar en el scoring

Dos decisiones propias de este dataset:

  - **Los faltantes son un tramo mas.** Aca los NaN son estructurales (la cuenta no usa ese
    canal de delivery), asi que pasar de faltar a no faltar es informacion, no ruido. Si se
    los ignorara, una columna que el DW deja de mandar daria PSI 0.
  - **La referencia son los meses de entrenamiento**, no el mes anterior: es contra esos
    datos que el modelo aprendio, y es lo que deja de valer cuando el mundo se corre.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from churn.config import Config
from churn.models.artifact import ModelArtifact

logger = logging.getLogger(__name__)

# Evita dividir por cero cuando un tramo esta vacio en una de las dos muestras.
EPS = 1e-6

SIN_CAMBIOS = "sin_cambios"
MODERADO = "moderado"
ALTO = "alto"

FALTANTE = "__faltante__"


@dataclass
class DriftReport:
    """Resultado de comparar el mes actual contra los datos de entrenamiento."""

    tabla: pd.DataFrame
    periodos_referencia: list[int]
    periodo_actual: int
    n_referencia: int
    n_actual: int
    umbral_moderado: float = 0.10
    umbral_alto: float = 0.25
    generado_en: str = ""

    @property
    def altos(self) -> pd.DataFrame:
        return self.tabla[self.tabla["nivel"] == ALTO]

    @property
    def moderados(self) -> pd.DataFrame:
        return self.tabla[self.tabla["nivel"] == MODERADO]

    @property
    def hay_drift(self) -> bool:
        return not self.altos.empty

    def to_dict(self) -> dict[str, Any]:
        return {
            "periodo_actual": self.periodo_actual,
            "periodos_referencia": self.periodos_referencia,
            "n_referencia": self.n_referencia,
            "n_actual": self.n_actual,
            "umbral_moderado": self.umbral_moderado,
            "umbral_alto": self.umbral_alto,
            "hay_drift": self.hay_drift,
            "conteo": {
                ALTO: int(len(self.altos)),
                MODERADO: int(len(self.moderados)),
                SIN_CAMBIOS: int((self.tabla["nivel"] == SIN_CAMBIOS).sum()),
            },
            "features": self.tabla.to_dict(orient="records"),
        }

    def render(self, top: int = 15) -> str:
        lineas = [
            f"  Referencia   periodos {self.periodos_referencia[0]}-{self.periodos_referencia[-1]}"
            f" ({self.n_referencia:,} filas)",
            f"  Actual       periodo {self.periodo_actual} ({self.n_actual:,} filas)",
            f"  Features     {len(self.tabla)} comparadas"
            f" | {len(self.altos)} con cambio alto"
            f" | {len(self.moderados)} moderado",
        ]
        if self.hay_drift:
            lineas.append(
                f"  Atencion: hay features con PSI >= {self.umbral_alto:.2f}. Las predicciones "
                "de este mes pueden no ser confiables; conviene reentrenar."
            )
        else:
            lineas.append(
                f"  Sin cambios grandes: ninguna feature supera un PSI de {self.umbral_alto:.2f}."
            )
        return "\n".join(lineas)


@dataclass
class DriftConfig:
    bins: int = 10
    umbral_moderado: float = 0.10
    umbral_alto: float = 0.25
    top_n: int = 15
    exclude_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Config) -> DriftConfig:
        return cls(
            bins=int(cfg.get("monitoring.psi_bins", 10)),
            umbral_moderado=float(cfg.get("monitoring.umbral_moderado", 0.10)),
            umbral_alto=float(cfg.get("monitoring.umbral_alto", 0.25)),
            top_n=int(cfg.get("monitoring.top_n", 15)),
            exclude_patterns=list(cfg.get("monitoring.exclude_patterns") or []),
        )


def _psi_proporciones(referencia: np.ndarray, actual: np.ndarray) -> float:
    """PSI entre dos vectores de proporciones ya alineados tramo a tramo."""
    ref = np.clip(np.asarray(referencia, dtype="float64"), EPS, None)
    act = np.clip(np.asarray(actual, dtype="float64"), EPS, None)
    return float(np.sum((act - ref) * np.log(act / ref)))


def psi_numerico(referencia: pd.Series, actual: pd.Series, bins: int = 10) -> float:
    """PSI de una variable numerica, con los faltantes como un tramo aparte."""
    ref_faltantes = float(referencia.isna().mean()) if len(referencia) else 0.0
    act_faltantes = float(actual.isna().mean()) if len(actual) else 0.0

    ref_valores = pd.to_numeric(referencia.dropna(), errors="coerce").dropna().to_numpy(float)
    act_valores = pd.to_numeric(actual.dropna(), errors="coerce").dropna().to_numpy(float)

    # Sin valores observados, o con una variable constante, los tramos no dicen nada: lo
    # unico comparable es cuanto falta.
    bordes = (
        np.unique(np.quantile(ref_valores, np.linspace(0, 1, bins + 1)))
        if len(ref_valores)
        else np.array([])
    )
    if len(ref_valores) == 0 or len(act_valores) == 0 or len(bordes) < 3:
        return _psi_proporciones(
            np.array([ref_faltantes, 1 - ref_faltantes]),
            np.array([act_faltantes, 1 - act_faltantes]),
        )

    # Los extremos se abren: un valor mas alto que todo lo visto en entrenamiento tiene que
    # caer en el ultimo tramo y no quedar afuera del conteo.
    bordes = np.concatenate(([-np.inf], bordes[1:-1], [np.inf]))
    ref_hist, _ = np.histogram(ref_valores, bins=bordes)
    act_hist, _ = np.histogram(act_valores, bins=bordes)

    # Las proporciones se calculan sobre el total de filas, faltantes incluidos, para que
    # el tramo de faltantes entre en la misma cuenta.
    ref_prop = np.append(ref_hist / len(referencia), ref_faltantes)
    act_prop = np.append(act_hist / len(actual), act_faltantes)
    return _psi_proporciones(ref_prop, act_prop)


def psi_categorico(referencia: pd.Series, actual: pd.Series) -> float:
    """PSI de una variable categorica: compara la frecuencia de cada categoria."""
    ref_freq = _frecuencias(referencia)
    act_freq = _frecuencias(actual)
    categorias = ref_freq.index.union(act_freq.index)
    return _psi_proporciones(
        ref_freq.reindex(categorias, fill_value=0.0).to_numpy(),
        act_freq.reindex(categorias, fill_value=0.0).to_numpy(),
    )


def _frecuencias(serie: pd.Series) -> pd.Series:
    if serie.empty:
        return pd.Series(dtype="float64")
    valores = serie.astype("object").where(serie.notna(), FALTANTE)
    return valores.value_counts(normalize=True)


def nivel(psi: float, umbral_moderado: float, umbral_alto: float) -> str:
    if psi >= umbral_alto:
        return ALTO
    if psi >= umbral_moderado:
        return MODERADO
    return SIN_CAMBIOS


def compute_drift(
    features: pd.DataFrame,
    artifact: ModelArtifact,
    cfg: Config,
    periodo: int | None = None,
) -> DriftReport:
    """Compara el periodo pedido (por defecto el ultimo) contra los meses de entrenamiento."""
    opciones = DriftConfig.from_config(cfg)
    periodo = int(periodo if periodo is not None else features["periodo"].max())

    actual = features[features["periodo"] == periodo]
    if actual.empty:
        raise ValueError(f"No hay filas del periodo {periodo} en las features.")

    periodos_referencia = [p for p in artifact.training_periods if p != periodo]
    if not periodos_referencia:
        raise ValueError(
            "El artefacto no registra periodos de entrenamiento: reentrenar con `churn train`."
        )
    referencia = features[features["periodo"].isin(periodos_referencia)]
    if referencia.empty:
        raise ValueError(
            "Las features no tienen los periodos con los que se entreno el modelo "
            f"({periodos_referencia[0]}-{periodos_referencia[-1]}). "
            "Puede ser un snapshot recortado: correr `churn prepare --force`."
        )

    columnas = [c for c in artifact.feature_names if c in features.columns]
    faltantes = len(artifact.feature_names) - len(columnas)
    if faltantes:
        logger.warning("%s features del modelo no estan en el panel actual", faltantes)
    if opciones.exclude_patterns:
        columnas = [c for c in columnas if not any(p in c for p in opciones.exclude_patterns)]

    filas = []
    for col in columnas:
        es_categorica = isinstance(features[col].dtype, pd.CategoricalDtype) or not (
            pd.api.types.is_numeric_dtype(features[col])
        )
        if es_categorica:
            valor = psi_categorico(referencia[col], actual[col])
        else:
            valor = psi_numerico(referencia[col], actual[col], opciones.bins)
        filas.append(
            {
                "feature": col,
                "tipo": "categorica" if es_categorica else "numerica",
                "psi": round(float(valor), 4),
                "nivel": nivel(valor, opciones.umbral_moderado, opciones.umbral_alto),
            }
        )

    tabla = pd.DataFrame(filas).sort_values("psi", ascending=False).reset_index(drop=True)
    reporte = DriftReport(
        tabla=tabla,
        periodos_referencia=sorted(periodos_referencia),
        periodo_actual=periodo,
        n_referencia=int(len(referencia)),
        n_actual=int(len(actual)),
        umbral_moderado=opciones.umbral_moderado,
        umbral_alto=opciones.umbral_alto,
    )
    logger.info(
        "Drift del periodo %s contra %s-%s: %s features con cambio alto, %s moderado",
        periodo,
        reporte.periodos_referencia[0],
        reporte.periodos_referencia[-1],
        len(reporte.altos),
        len(reporte.moderados),
    )
    return reporte
