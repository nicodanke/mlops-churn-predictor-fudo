"""Estadistica descriptiva del panel: salud de la base, adopcion y señales de churn.

Es la version ejecutable del EDA del notebook. La diferencia es el destino: el notebook
sirve para entender los datos una vez, esto corre todos los meses junto con el batch y
deja un JSON que consume el dashboard, de modo que CX vea contra que base esta mirando
las predicciones.

    churn eda   ->  outputs/eda/stats.json  ->  GET /api/v1/eda  ->  dashboard

Las tres secciones responden a tres preguntas distintas:

    base        cuantas cuentas hay, como crece la base y a que ritmo se va la gente
    adopcion    que parte del producto usa realmente una cuenta promedio
    churn       en que se diferencia una cuenta que se va de una que se queda
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from churn.config import Config
from churn.data.labeling import churn_rate_by_period
from churn.data.seasonality import SeasonalityConfig, account_seasonality
from churn.eda.catalog import FUNCIONALIDADES, GRUPOS, SENALES_CHURN, VOLUMEN

logger = logging.getLogger(__name__)

# Debajo de esta cantidad de filas etiquetadas una tasa no se reporta: el ruido supera
# la señal. Se cuenta en meses-cuenta y no en cuentas porque es sobre lo que se promedia:
# un pais con 20 cuentas observadas 5 meses aporta 100 observaciones, no 20.
MIN_FILAS_PARA_TASA = 100


def build_report(features: pd.DataFrame, cfg: Config | None = None) -> dict[str, Any]:
    """Arma el reporte completo de EDA a partir de la matriz de features etiquetada."""
    periodos = sorted(int(p) for p in features["periodo"].unique())
    ultimo = periodos[-1]

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "panel": {
            "filas": int(len(features)),
            "cuentas": int(features["id"].nunique()),
            "periodos": len(periodos),
            "periodo_desde": periodos[0],
            "periodo_hasta": ultimo,
            "periodo_referencia": ultimo,
        },
        "base": _salud_de_la_base(features, cfg),
        "adopcion": _adopcion(features, ultimo),
        "churn": _uso_vs_churn(features),
    }
    logger.info(
        "Reporte de EDA: %s cuentas, %s periodos, churn base %.2f%%",
        f"{report['panel']['cuentas']:,}",
        report["panel"]["periodos"],
        report["base"]["churn_rate_promedio"],
    )
    return report


# ---------------------------------------------------------------------------
# 1. Salud de la base
# ---------------------------------------------------------------------------


def _salud_de_la_base(features: pd.DataFrame, cfg: Config | None) -> dict[str, Any]:
    """Cuentas activas, altas, bajas y churn rate mes a mes.

    "Alta" es la primera aparicion de una cuenta en el panel, salvo en el primer
    periodo: ahi todas las cuentas aparecen por primera vez porque ahi empieza la
    ventana de observacion, no porque se hayan dado de alta ese mes.

    "Baja confirmada" son las filas con churn=1: la cuenta estaba en ese mes y ya no
    aparece en ninguno de los siguientes, dentro de la ventana de confirmacion. Por eso
    los ultimos meses del panel no tienen bajas: todavia no se pueden confirmar.
    """
    activas = features.groupby("periodo").size()
    primer_periodo = int(activas.index.min())

    altas = (
        features.loc[features["months_in_panel"] == 1]
        .groupby("periodo")
        .size()
        .reindex(activas.index, fill_value=0)
    )
    altas.loc[primer_periodo] = 0  # la cohorte inicial no son altas del mes

    etiquetadas = features[features["churn"].notna()]
    bajas = etiquetadas.groupby("periodo")["churn"].sum().reindex(activas.index)
    evaluables = etiquetadas.groupby("periodo").size().reindex(activas.index)

    serie = []
    for periodo in activas.index:
        n_eval = evaluables.get(periodo)
        tiene_etiqueta = pd.notna(n_eval) and n_eval > 0
        serie.append(
            {
                "periodo": int(periodo),
                "cuentas_activas": int(activas[periodo]),
                "altas": int(altas[periodo]),
                "bajas_confirmadas": int(bajas[periodo]) if tiene_etiqueta else None,
                "cuentas_evaluables": int(n_eval) if tiene_etiqueta else None,
                "churn_rate": (
                    round(float(bajas[periodo]) / float(n_eval) * 100, 2)
                    if tiene_etiqueta
                    else None
                ),
            }
        )

    tasas = churn_rate_by_period(features)
    # Promedio ponderado por cuentas evaluables, no promedio de promedios: los meses
    # con mas base tienen que pesar mas.
    n_etiquetadas = len(etiquetadas)
    churn_promedio = float(etiquetadas["churn"].mean()) if n_etiquetadas else 0.0

    ultimo, primero = int(activas.iloc[-1]), int(activas.iloc[0])
    meses = len(activas) - 1
    crecimiento_mensual = (ultimo / primero) ** (1 / meses) - 1 if meses and primero else 0.0

    salida: dict[str, Any] = {
        "serie": serie,
        "cuentas_activas_ultimo": ultimo,
        "cuentas_activas_primero": primero,
        "crecimiento_total": round((ultimo / primero - 1) * 100, 2) if primero else None,
        "crecimiento_mensual": round(crecimiento_mensual * 100, 2),
        "altas_totales": int(altas.sum()),
        "bajas_confirmadas_totales": int(bajas.sum(skipna=True)),
        "churn_rate_promedio": round(churn_promedio * 100, 2),
        "churn_rate_min": round(float(tasas["churn_rate"].min()), 2) if len(tasas) else None,
        "churn_rate_max": round(float(tasas["churn_rate"].max()), 2) if len(tasas) else None,
        "filas_etiquetadas": int(n_etiquetadas),
        "filas_ambiguas": int(features["is_ambiguous"].sum()),
        # Con un churn mensual r, una cuenta dura en promedio 1/r meses y la base
        # retiene (1-r)^12 al cabo de un año. Son las dos lecturas que pide negocio.
        "retencion_anual": round(((1 - churn_promedio) ** 12) * 100, 1) if churn_promedio else None,
        "vida_media_meses": round(1 / churn_promedio, 1) if churn_promedio else None,
        "periodos_sin_etiqueta": [
            int(p) for p in activas.index if pd.isna(evaluables.get(p)) or evaluables.get(p) == 0
        ],
    }
    salida.update(_estacionales(features, cfg))
    salida["paises"] = _paises(features)
    return salida


def _estacionales(features: pd.DataFrame, cfg: Config | None) -> dict[str, Any]:
    """Cuantas cuentas pausan y vuelven. Con un panel corto el detector ve menos."""
    seasonal_cfg = SeasonalityConfig.from_config(cfg) if cfg is not None else SeasonalityConfig()
    perfil = account_seasonality(features[["id", "t", "periodo"]], seasonal_cfg)
    total = int(features["id"].nunique())
    if perfil.empty:
        return {"cuentas_con_pausas": 0, "cuentas_estacionales": 0, "pct_estacionales": 0.0}
    estacionales = perfil[perfil["es_estacional"]]
    return {
        "cuentas_con_pausas": int(len(perfil)),
        "cuentas_estacionales": int(len(estacionales)),
        "pct_estacionales": round(len(estacionales) / total * 100, 2) if total else 0.0,
    }


def _paises(features: pd.DataFrame, top: int = 8) -> list[dict[str, Any]]:
    """Reparto de la base por pais en el ultimo periodo, con su churn rate propio."""
    if "pais" not in features.columns:
        return []
    ultimo = int(features["periodo"].max())
    actual = features[features["periodo"] == ultimo]
    cuentas = actual["pais"].astype(str).value_counts()

    etiquetadas = features[features["churn"].notna()]
    churn_por_pais = etiquetadas.groupby(etiquetadas["pais"].astype(str))["churn"].agg(
        ["mean", "size"]
    )

    filas = []
    for pais, n in cuentas.head(top).items():
        fila = churn_por_pais.loc[pais] if pais in churn_por_pais.index else None
        suficiente = fila is not None and fila["size"] >= MIN_FILAS_PARA_TASA
        filas.append(
            {
                "pais": pais,
                "cuentas": int(n),
                "pct_base": round(n / len(actual) * 100, 2),
                "churn_rate": round(float(fila["mean"]) * 100, 2) if suficiente else None,
            }
        )
    return filas


# ---------------------------------------------------------------------------
# 2. Adopcion del producto
# ---------------------------------------------------------------------------


def _adopcion(features: pd.DataFrame, periodo: int) -> dict[str, Any]:
    """Que parte del producto usa una cuenta, medido sobre el ultimo mes cerrado.

    "Usa" es tener un valor mayor a cero en el mes. Es una definicion generosa — una
    sola venta por PedidosYa cuenta igual que mil — pero es la que responde la pregunta
    que importa para CX: de que funcionalidades hay rastro en la cuenta.
    """
    actual = features[features["periodo"] == periodo]
    n = len(actual)
    if not n:
        return {"periodo": periodo, "cuentas": 0, "grupos": [], "volumen": []}

    por_grupo: dict[str, list[dict[str, Any]]] = {g: [] for g in GRUPOS}
    for func in FUNCIONALIDADES:
        if func.columna not in actual.columns:
            continue
        valores = actual[func.columna]
        usan = int((valores.fillna(0) > 0).sum())
        entre_usuarios = valores[valores > 0]
        por_grupo.setdefault(func.grupo, []).append(
            {
                "columna": func.columna,
                "etiqueta": func.etiqueta,
                "cuentas": usan,
                "pct": round(usan / n * 100, 2),
                # Mediana entre quienes la usan: separa "poca gente la usa" de
                # "mucha gente la usa poco".
                "mediana_entre_usuarios": (
                    _num(entre_usuarios.median()) if len(entre_usuarios) else None
                ),
            }
        )

    grupos = [
        {
            "grupo": nombre,
            "items": sorted(items, key=lambda x: x["pct"], reverse=True),
        }
        for nombre in GRUPOS
        if (items := por_grupo.get(nombre))
    ]

    return {
        "periodo": periodo,
        "cuentas": n,
        "grupos": grupos,
        "volumen": [
            {
                "columna": col,
                "etiqueta": etiqueta,
                "mediana": _num(actual[col].median()),
                "p25": _num(actual[col].quantile(0.25)),
                "p75": _num(actual[col].quantile(0.75)),
                "p95": _num(actual[col].quantile(0.95)),
            }
            for col, etiqueta in VOLUMEN
            if col in actual.columns
        ],
    }


# ---------------------------------------------------------------------------
# 3. Uso vs churn
# ---------------------------------------------------------------------------


def _uso_vs_churn(features: pd.DataFrame) -> dict[str, Any]:
    """En que se diferencia, el mes previo, una cuenta que se va de una que se queda.

    Se reportan dos lecturas por metrica porque cada una miente por su lado:

        adopcion  que porcentaje de cada grupo tiene la funcionalidad en uso. Robusta,
                  pero ciega a la intensidad.
        mediana   cuanto la usa el grupo. Informativa, pero cae a cero en cuanto mas de
                  la mitad de las cuentas no usa la funcionalidad.

    El `lift` es el cociente de adopcion: 0.60 significa que entre las cuentas que se
    van la funcionalidad aparece un 40% menos seguido que entre las que se quedan.
    """
    etiquetadas = features[features["churn"].notna()]
    if etiquetadas.empty:
        return {"n_se_queda": 0, "n_churn": 0, "senales": [], "estado_cobranza": []}

    se_queda = etiquetadas[etiquetadas["churn"] == 0]
    churnea = etiquetadas[etiquetadas["churn"] == 1]

    senales = []
    for col, etiqueta in SENALES_CHURN:
        if col not in etiquetadas.columns:
            continue
        adop_queda = float((se_queda[col].fillna(0) > 0).mean())
        adop_churn = float((churnea[col].fillna(0) > 0).mean())
        senales.append(
            {
                "columna": col,
                "etiqueta": etiqueta,
                "adopcion_se_queda": round(adop_queda * 100, 2),
                "adopcion_churn": round(adop_churn * 100, 2),
                "lift": round(adop_churn / adop_queda, 3) if adop_queda else None,
                "mediana_se_queda": _mediana(se_queda[col]),
                "mediana_churn": _mediana(churnea[col]),
            }
        )
    # De la señal mas negativa a la mas neutra: lo primero que mira CX es que se apaga
    # antes de una baja.
    senales.sort(key=lambda s: s["lift"] if s["lift"] is not None else 1.0)

    return {
        "n_se_queda": int(len(se_queda)),
        "n_churn": int(len(churnea)),
        "churn_rate": round(float(etiquetadas["churn"].mean()) * 100, 2),
        "senales": senales,
        "estado_cobranza": _estado_cobranza(etiquetadas),
        "antiguedad": _churn_por_antiguedad(etiquetadas),
    }


def _estado_cobranza(etiquetadas: pd.DataFrame) -> list[dict[str, Any]]:
    """Churn rate por estado de cuenta. Es el separador mas fuerte y el mas tardio."""
    if "estado" not in etiquetadas.columns:
        return []
    agrupado = etiquetadas.groupby(etiquetadas["estado"].astype(str))["churn"].agg(["mean", "size"])
    filas = [
        {
            "estado": estado,
            "cuentas": int(fila["size"]),
            "churn_rate": round(float(fila["mean"]) * 100, 2),
        }
        for estado, fila in agrupado.iterrows()
        if fila["size"] >= MIN_FILAS_PARA_TASA
    ]
    return sorted(filas, key=lambda f: f["churn_rate"], reverse=True)


def _churn_por_antiguedad(etiquetadas: pd.DataFrame) -> list[dict[str, Any]]:
    """Churn rate por tramo de antiguedad: cuando se va la gente que se va."""
    if "tenure_months" not in etiquetadas.columns:
        return []
    cortes = [0, 3, 6, 12, 24, 48, np.inf]
    nombres = ["0-3 meses", "3-6 meses", "6-12 meses", "1-2 años", "2-4 años", "4+ años"]
    tramos = pd.cut(etiquetadas["tenure_months"], bins=cortes, labels=nombres, right=False)
    agrupado = etiquetadas.groupby(tramos, observed=True)["churn"].agg(["mean", "size"])
    return [
        {
            "tramo": str(tramo),
            "cuentas": int(fila["size"]),
            "churn_rate": round(float(fila["mean"]) * 100, 2),
        }
        for tramo, fila in agrupado.iterrows()
        if fila["size"] >= MIN_FILAS_PARA_TASA
    ]


def _mediana(serie: pd.Series) -> float | None:
    """Mediana de una columna que puede estar entera en null.

    Pasa cuando ningun miembro del grupo usa la funcionalidad — el caso mas informativo
    del reporte, justamente. La mediana de la nada es None, no cero.
    """
    return _num(serie.median()) if serie.notna().any() else None


def _num(value: Any) -> float | None:
    """Convierte un escalar de numpy/pandas a float JSON-serializable."""
    if value is None or pd.isna(value):
        return None
    return round(float(value), 2)
