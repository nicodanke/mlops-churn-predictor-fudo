"""Deteccion de cuentas con estacionalidad de uso.

El ML Canvas pide excluir del entrenamiento "cuentas con estacionalidad de uso (altas y
bajas recurrentes) que distorsionan la señal". Este modulo las identifica.

Un restaurante de temporada — parador de playa, refugio de montaña, local en una zona
turistica — cierra varios meses y reabre. En el snapshot eso se ve igual que una baja:
la cuenta desaparece. Pero no es una baja, y entrenar con esas filas le enseña al modelo
que "dejar de operar en mayo" es churn, cuando para esas cuentas es lo normal.

Que muestran los datos (panel 202401-202603, 56.202 cuentas):

    - 3.727 cuentas (6,6%) pausaron al menos una vez.
    - Solo 793 pausaron dos o mas veces: ese es el patron realmente recurrente.
    - Las pausas arrancan en abril-mayo (indice 1,19 y 1,30 sobre lo uniforme) y las
      reactivaciones se concentran en octubre-diciembre (1,22 / 1,20 / 1,48).
      Es la firma del invierno del hemisferio sur.
    - Argentina y Chile concentran 41,6% y 39,6% de sus pausas en abril-julio, contra
      31,8% de Mexico: el patron es geografico, no del producto.

De ahi los dos criterios de deteccion: recurrencia (pauso mas de una vez) o una unica
pausa con firma de temporada (se fue en otoño, volvio en primavera, duro una temporada).

IMPORTANTE — esto mira todo el panel, incluido el futuro de cada fila. Sirve para
*limpiar el set de entrenamiento*, que es un uso legitimo, pero NUNCA puede entrar como
feature del modelo: en produccion no se sabe si una cuenta va a resultar estacional.
La version causal de esta señal es `pausas_previas`, que solo mira hacia atras.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from churn.data.loader import next_period

logger = logging.getLogger(__name__)

# Meses en los que arranca la temporada baja en el hemisferio sur (otoño-invierno).
MESES_BAJA_TEMPORADA = (4, 5, 6, 7)
# Meses de reapertura (primavera-verano).
MESES_ALTA_TEMPORADA = (10, 11, 12, 1, 2, 3)

NOMBRE_MES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


@dataclass
class SeasonalityConfig:
    """Reglas para marcar una cuenta como estacional."""

    # Cantidad de pausas que alcanza para considerar el patron recurrente.
    min_episodios: int = 2
    # Una sola pausa alcanza si tiene firma de temporada (se fue en otoño, volvio
    # en primavera) y duro entre estos limites.
    duracion_min_temporada: int = 3
    duracion_max_temporada: int = 8
    aceptar_firma_unica: bool = True

    @classmethod
    def from_config(cls, cfg) -> SeasonalityConfig:
        raw = cfg.get("seasonality", {}) or {}
        return cls(
            min_episodios=int(raw.get("min_episodios", 2)),
            duracion_min_temporada=int(raw.get("duracion_min_temporada", 3)),
            duracion_max_temporada=int(raw.get("duracion_max_temporada", 8)),
            aceptar_firma_unica=bool(raw.get("aceptar_firma_unica", True)),
        )


def find_pause_episodes(panel: pd.DataFrame) -> pd.DataFrame:
    """Episodios de ausencia que terminaron en un retorno.

    Un episodio es un tramo de meses ausentes *entre* dos apariciones. La ultima
    desaparicion de una cuenta no cuenta: puede ser una baja o una pausa todavia
    en curso, y no hay forma de saberlo.

    Columnas: id, t_baja (ultimo mes visto), t_alta (mes del retorno), duracion,
    periodo_baja / periodo_alta y el mes calendario de cada punta.
    """
    ordenado = panel[["id", "t"]].drop_duplicates().sort_values(["id", "t"])
    ids = ordenado["id"].to_numpy()
    ts = ordenado["t"].to_numpy()

    # Un episodio existe donde el salto entre dos meses consecutivos de la misma
    # cuenta es mayor a 1.
    misma_cuenta = ids[1:] == ids[:-1]
    salto = ts[1:] - ts[:-1]
    hay_hueco = misma_cuenta & (salto > 1)

    episodios = pd.DataFrame(
        {
            "id": ids[:-1][hay_hueco],
            "t_baja": ts[:-1][hay_hueco],
            "t_alta": ts[1:][hay_hueco],
        }
    )
    episodios["duracion"] = episodios["t_alta"] - episodios["t_baja"] - 1

    calendario = _calendario(panel)
    # El mes de baja es el primero en que la cuenta ya no aparece.
    episodios["periodo_baja"] = episodios["t_baja"].add(1).map(calendario)
    episodios["periodo_alta"] = episodios["t_alta"].map(calendario)
    episodios["mes_baja"] = _mes(episodios["periodo_baja"])
    episodios["mes_alta"] = _mes(episodios["periodo_alta"])

    logger.info(
        "Episodios de pausa detectados: %s sobre %s cuentas",
        f"{len(episodios):,}",
        f"{episodios['id'].nunique():,}",
    )
    return episodios.reset_index(drop=True)


def account_seasonality(
    panel: pd.DataFrame, config: SeasonalityConfig | None = None
) -> pd.DataFrame:
    """Perfil de estacionalidad por cuenta.

    Devuelve una fila por cuenta que pauso alguna vez, con:
        n_pausas          cuantas veces se fue y volvio
        duracion_media    meses promedio de cada pausa
        mes_baja_modal    mes calendario en que suele irse
        mes_alta_modal    mes calendario en que suele volver
        firma_temporada   el patron coincide con la temporada del hemisferio sur
        mismo_mes         repite la pausa en el mismo mes del año (+/- 1)
        es_estacional     veredicto final segun las reglas de `config`
    """
    config = config or SeasonalityConfig()
    episodios = find_pause_episodes(panel)

    if episodios.empty:
        return pd.DataFrame(
            columns=[
                "id", "n_pausas", "duracion_media", "duracion_total",
                "mes_baja_modal", "mes_alta_modal", "firma_temporada",
                "mismo_mes", "es_estacional",
            ]
        )

    perfil = episodios.groupby("id").agg(
        n_pausas=("duracion", "size"),
        duracion_media=("duracion", "mean"),
        duracion_total=("duracion", "sum"),
        mes_baja_modal=("mes_baja", _moda),
        mes_alta_modal=("mes_alta", _moda),
    )
    perfil["duracion_media"] = perfil["duracion_media"].round(1)

    # ¿Las pausas caen siempre en la misma epoca del año?
    perfil["mismo_mes"] = episodios.groupby("id")["mes_baja"].apply(_meses_cercanos)

    # Firma de temporada: se va en otoño, vuelve en primavera y dura una temporada.
    perfil["firma_temporada"] = (
        perfil["mes_baja_modal"].isin(MESES_BAJA_TEMPORADA)
        & perfil["mes_alta_modal"].isin(MESES_ALTA_TEMPORADA)
        & perfil["duracion_media"].between(
            config.duracion_min_temporada, config.duracion_max_temporada
        )
    )

    recurrente = perfil["n_pausas"] >= config.min_episodios
    perfil["es_estacional"] = (
        recurrente | (perfil["firma_temporada"] if config.aceptar_firma_unica else False)
    )

    # Bajo "estacional" caen dos fenomenos distintos que conviene no confundir:
    #
    #   temporada    el negocio cierra media temporada y reabre. Es el parador de playa
    #                del canvas: baja en otoño, alta en primavera, tres meses o mas.
    #   intermitente pausas cortas y repetidas sin patron de calendario. No es un negocio
    #                de temporada; suele ser uso erratico o idas y vueltas de cobranza.
    #
    # Los dos son ruido para el modelo de churn y se excluyen igual, pero para negocio
    # significan cosas distintas: al de temporada no hay que ir a rescatarlo en mayo.
    perfil["tipo"] = np.where(
        perfil["firma_temporada"], "temporada", np.where(recurrente, "intermitente", "-")
    )

    perfil = perfil.reset_index()
    conteo = perfil.loc[perfil["es_estacional"], "tipo"].value_counts()
    logger.info(
        "Cuentas estacionales: %s (%s de temporada, %s intermitentes)",
        f"{int(perfil['es_estacional'].sum()):,}",
        f"{int(conteo.get('temporada', 0)):,}",
        f"{int(conteo.get('intermitente', 0)):,}",
    )
    return perfil


def seasonal_account_ids(
    panel: pd.DataFrame, config: SeasonalityConfig | None = None
) -> set[int]:
    """Ids de las cuentas a excluir del entrenamiento."""
    perfil = account_seasonality(panel, config)
    if perfil.empty:
        return set()
    return set(perfil.loc[perfil["es_estacional"], "id"].tolist())


# ---------------------------------------------------------------------------
# Reportes: el "dato extra" para entender el ciclo de altas y bajas
# ---------------------------------------------------------------------------


def monthly_profile(panel: pd.DataFrame, min_future_months: int = 3) -> pd.DataFrame:
    """Distribucion por mes calendario de bajas definitivas, pausas y reactivaciones.

    El indice compara contra lo que se esperaria si el fenomeno fuera uniforme a lo
    largo del año: 1,00 es "lo normal", 1,30 es "30% mas de lo esperable en ese mes".
    """
    calendario = _calendario(panel)
    max_t = int(panel["t"].max())
    episodios = find_pause_episodes(panel)

    apariciones = panel.groupby("id")["t"]
    ultima = apariciones.max()
    primera = apariciones.min()

    # Baja definitiva: no vuelve a aparecer y hay margen de observacion suficiente.
    ids_pausa = set(zip(episodios["id"], episodios["t_baja"], strict=True))
    bajas = [
        t + 1
        for acct, t in ultima.items()
        if t <= max_t - min_future_months and (acct, t) not in ids_pausa
    ]

    # Alta nueva: primera aparicion en el panel, salteando t=0 (que es el arranque
    # del panel y no un alta real).
    altas = [t for t in primera.to_numpy() if t > 0]

    return pd.concat(
        [
            _distribucion(bajas, calendario, "baja definitiva"),
            _distribucion(episodios["t_baja"].add(1).tolist(), calendario, "inicio de pausa"),
            _distribucion(episodios["t_alta"].tolist(), calendario, "reactivacion"),
            _distribucion(altas, calendario, "alta nueva"),
        ],
        ignore_index=True,
    )


def seasonality_by_country(panel: pd.DataFrame, min_accounts: int = 300) -> pd.DataFrame:
    """Tasa de pausa y concentracion en temporada baja, por pais."""
    episodios = find_pause_episodes(panel)
    pais_de = panel.groupby("id")["pais"].last()
    episodios["pais"] = episodios["id"].map(pais_de)

    total = panel.groupby("pais")["id"].nunique().rename("cuentas")
    con_pausa = episodios.groupby("pais")["id"].nunique().rename("con_pausa")

    tabla = pd.concat([total, con_pausa], axis=1).fillna(0)
    tabla = tabla[tabla["cuentas"] >= min_accounts]
    tabla["pct_pausan"] = (tabla["con_pausa"] / tabla["cuentas"] * 100).round(2)

    en_temporada = episodios["mes_baja"].isin(MESES_BAJA_TEMPORADA)
    tabla["pausas_en_temporada_baja_%"] = (
        en_temporada.groupby(episodios["pais"]).mean() * 100
    ).round(1)

    tabla["con_pausa"] = tabla["con_pausa"].astype(int)
    return tabla.sort_values("pct_pausan", ascending=False).reset_index()


# ---------------------------------------------------------------------------


def _calendario(panel: pd.DataFrame) -> dict[int, int]:
    """Mapa t -> periodo YYYYMM, denso sobre todo el rango.

    No alcanza con los pares observados: el mes de baja de un episodio es `t_baja + 1`,
    que por definicion es un mes en el que esa cuenta no aparece. Si ademas ninguna otra
    cuenta lo tuviera — un panel chico, un mes faltante en el snapshot — ese t no estaria
    en el mapa y el mes quedaria nulo sin que nadie se entere. Se completa por aritmetica.
    """
    pares = panel[["t", "periodo"]].drop_duplicates().sort_values("t")
    observado = dict(zip(pares["t"].astype(int), pares["periodo"].astype(int), strict=True))
    if not observado:
        return {}

    t_min, t_max = min(observado), max(observado)
    completo: dict[int, int] = {}
    periodo = observado[t_min]
    # Se extiende un mes mas alla del final: `t_baja + 1` puede caer justo despues.
    for t in range(t_min, t_max + 2):
        completo[t] = observado.get(t, periodo)
        periodo = next_period(completo[t])
    return completo


def _mes(periodos: pd.Series) -> pd.Series:
    return (periodos.astype("Int64") % 100).astype("Int64")


def _moda(serie: pd.Series) -> int:
    modas = serie.mode()
    return int(modas.iloc[0]) if len(modas) else 0


def _meses_cercanos(meses: pd.Series) -> bool:
    """True si todas las pausas de la cuenta caen en el mismo mes del año, +/- 1."""
    valores = meses.dropna().astype(int).unique()
    if len(valores) <= 1:
        return True
    # Distancia circular: diciembre y enero estan a 1 mes, no a 11.
    for a in valores:
        if all(min((a - b) % 12, (b - a) % 12) <= 1 for b in valores):
            return True
    return False


def _distribucion(ts: list[int], calendario: dict[int, int], etiqueta: str) -> pd.DataFrame:
    meses = pd.Series([calendario[t] % 100 for t in ts if t in calendario], dtype="Int64")
    conteo = meses.value_counts().reindex(range(1, 13), fill_value=0).sort_index()
    total = int(conteo.sum())
    esperado = total / 12 if total else 1

    return pd.DataFrame(
        {
            "evento": etiqueta,
            "mes": [NOMBRE_MES[m - 1] for m in conteo.index],
            "n": conteo.to_numpy(),
            "pct": (conteo.to_numpy() / max(total, 1) * 100).round(1),
            "indice": np.round(conteo.to_numpy() / esperado, 2),
        }
    )
