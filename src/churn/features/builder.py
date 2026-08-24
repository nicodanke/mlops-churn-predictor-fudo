"""Feature engineering sobre el panel mensual de cuentas.

La intuicion del problema: el valor absoluto de una metrica dice poco (un bar chico
factura menos que una cadena y ninguno de los dos esta churneando por eso). Lo que
anticipa una baja es el *movimiento*: ventas que caen, usuarios que dejan de entrar,
arqueos que se dejan de hacer, la cuenta que pasa a PENDING_PAYMENT.

Por eso, ademas del nivel actual, se construyen para cada metrica:

    <col>_delta_1m      variacion absoluta contra el mes anterior
    <col>_ratio_3m      valor actual sobre el promedio de los 3 meses previos
    <col>_delta_3m      variacion contra hace 3 meses (solo metricas nucleo)
    <col>_slope_3m      pendiente de la recta de los ultimos 3 meses (solo nucleo)

Los lags se calculan con un join sobre (id, t - lag) y no con `groupby().shift()`:
hay cuentas con huecos en el panel y un shift ciego tomaria el mes equivocado.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from churn.data.schema import (
    ADDITION_COLS,
    CORE_USAGE_COLS,
    DROP_FROM_FEATURES,
    ID_COLS,
    SALES_COLS,
)
from churn.pricing.plans import MODULES, parse_plan

logger = logging.getLogger(__name__)

# Columnas que existen en el panel pero no son features del modelo.
NON_FEATURE_COLS = (
    set(ID_COLS)
    | set(DROP_FROM_FEATURES)
    | {
        "t",
        "churn",
        "is_ambiguous",
        "is_labelable",
        "pais",
        "estado",
        "direcciones_google_maps",
    }
)


# Columnas numericas que no deben pasar por el motor de lags: o ya son una derivada
# temporal calculada a mano (y tendriamos un delta de un delta), o su evolucion mensual
# es trivial (tenure crece exactamente 1 por mes), o son atributos de contratacion cuyo
# cambio ya se modela en `_add_history_features`.
NO_LAG_COLS = {
    "t",
    "months_in_panel",
    "tenure_months",
    "estado_severity_max_3m",
    "estado_severity_delta_1m",
    "plan_rank",
    "plan_n_modulos",
}
NO_LAG_PREFIXES = ("plan_modulo_",)


def _numeric_base_columns(df: pd.DataFrame) -> list[str]:
    """Metricas numericas del snapshot a las que se les calculan lags."""
    out = []
    for col in df.columns:
        if col in NON_FEATURE_COLS or col in NO_LAG_COLS:
            continue
        if col.startswith(NO_LAG_PREFIXES):
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            out.append(col)
    return out


def build_features(
    panel: pd.DataFrame,
    lags: list[int] | None = None,
    rolling_window: int = 3,
) -> pd.DataFrame:
    """Devuelve el panel con todas las features derivadas agregadas."""
    lags = lags or [1, 2, 3]
    df = panel.sort_values(["id", "t"], kind="mergesort").reset_index(drop=True)

    df = _add_business_aggregates(df)
    df = _add_config_ratios(df)
    df = _add_plan_features(df)
    df = _add_state_features(df)

    numeric_cols = _numeric_base_columns(df)
    logger.info("Calculando temporales sobre %s metricas base", len(numeric_cols))
    df = _add_temporal_features(df, numeric_cols, lags, rolling_window)

    df = _add_history_features(df)

    logger.info("Matriz final: %s filas x %s features", f"{len(df):,}", len(feature_columns(df)))
    return df


# ---------------------------------------------------------------------------
# Agregados de negocio
# ---------------------------------------------------------------------------


def _add_business_aggregates(df: pd.DataFrame) -> pd.DataFrame:
    """Totales y mix de canales: como vende la cuenta, no solo cuanto."""
    sales = [c for c in SALES_COLS if c in df.columns]
    adds = [c for c in ADDITION_COLS if c in df.columns]

    sales_matrix = df[sales].fillna(0.0)
    df["ventas_totales"] = sales_matrix.sum(axis=1).astype("float32")
    df["canales_venta_activos"] = (sales_matrix > 0).sum(axis=1).astype("int8")

    delivery_cols = [
        c
        for c in (
            "v_delivery",
            "v_pedidosya",
            "v_ubereats",
            "v_justo",
            "v_rappi",
            "v_ifood",
            "v_didi",
        )
        if c in df.columns
    ]
    ventas_delivery = df[delivery_cols].fillna(0.0).sum(axis=1)
    total = df["ventas_totales"].replace(0, np.nan)
    df["ratio_ventas_delivery"] = (ventas_delivery / total).astype("float32")

    if "v_salon" in df.columns:
        df["ratio_ventas_salon"] = (df["v_salon"].fillna(0) / total).astype("float32")

    df["adiciones_totales"] = df[adds].fillna(0.0).sum(axis=1).astype("float32")
    df["sin_ventas_en_el_mes"] = (df["ventas_totales"] <= 0).astype("int8")

    # Amplitud de uso: de cuantas funcionalidades del producto hay rastro este mes.
    usage_cols = [c for c in CORE_USAGE_COLS if c in df.columns]
    df["funcionalidades_en_uso"] = (df[usage_cols].fillna(0) > 0).sum(axis=1).astype("int8")

    return df


def _add_config_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Que tan configurada esta la cuenta: una cuenta a medio configurar retiene peor."""

    def ratio(num: str, den: str, name: str) -> None:
        if num in df.columns and den in df.columns:
            denom = df[den].replace(0, np.nan)
            df[name] = (df[num] / denom).clip(0, 5).astype("float32")

    ratio("pr_con_stock", "productos", "ratio_productos_con_stock")
    ratio("pr_con_costo", "productos", "ratio_productos_con_costo")
    ratio("ing_con_stock", "ingredientes", "ratio_ingredientes_con_stock")
    ratio("ing_con_costo", "ingredientes", "ratio_ingredientes_con_costo")
    ratio("ing_en_recetas", "ingredientes", "ratio_ingredientes_en_recetas")
    ratio("usuarios_con_pin_asinado", "usuarios", "ratio_usuarios_con_pin")
    ratio("usuarios_con_cajas_asignadas", "usuarios", "ratio_usuarios_con_caja")
    ratio("ventas_con_clientes_asignados", "ventas_totales", "ratio_ventas_con_cliente")
    ratio("cat_gastos_con_cat_financiera", "cat_gastos", "ratio_cat_gastos_con_financiera")

    if "productos" in df.columns:
        df["productos_por_categoria"] = (
            df["productos"]
            / df.get("cat_productos", pd.Series(1, index=df.index)).replace(0, np.nan)
        ).astype("float32")

    return df


def _add_plan_features(df: pd.DataFrame) -> pd.DataFrame:
    """Descompone el plan code en plan base, modulos y mercado.

    El parseo se hace una sola vez por plan code distinto (hay ~2k, contra 700k filas).
    """
    codes = df["plan"].astype("string").fillna("")
    unique_codes = codes.unique()
    parsed = {code: parse_plan(code) for code in unique_codes}

    lookup = pd.DataFrame(
        {
            "plan": list(parsed.keys()),
            "plan_rank": [p.rank for p in parsed.values()],
            "plan_n_modulos": [p.n_modules for p in parsed.values()],
            "plan_mercado": [p.market or "desconocido" for p in parsed.values()],
            "plan_base": [p.base or "desconocido" for p in parsed.values()],
            **{f"plan_modulo_{m}": [int(m in p.modules) for p in parsed.values()] for m in MODULES},
        }
    )
    lookup["plan"] = lookup["plan"].astype("string")

    df = df.assign(plan=codes).merge(lookup, on="plan", how="left")

    df["plan_mercado"] = df["plan_mercado"].astype("category")
    df["plan_base"] = df["plan_base"].astype("category")
    df["plan_rank"] = df["plan_rank"].fillna(0).astype("int8")
    df["plan_n_modulos"] = df["plan_n_modulos"].fillna(0).astype("int8")

    if "pais" in df.columns:
        df["pais_cat"] = df["pais"].astype("category")

    return df


def _add_state_features(df: pd.DataFrame) -> pd.DataFrame:
    """El estado de cobranza es la señal mas directa de una baja inminente."""
    if "estado_severity" not in df.columns:
        return df

    grp = df.groupby("id", sort=False)["estado_severity"]
    df["estado_severity_max_3m"] = (
        grp.rolling(3, min_periods=1).max().reset_index(level=0, drop=True).astype("int8")
    )
    df["estado_severity_delta_1m"] = grp.diff().fillna(0).astype("int8")

    # Meses consecutivos con la cuenta fuera de ACTIVE (mora acumulada).
    con_problema = (df["estado_severity"] > 0).astype("int8")
    bloque = (con_problema != con_problema.groupby(df["id"]).shift(fill_value=0)).cumsum()
    racha = con_problema.groupby([df["id"], bloque]).cumsum()
    df["meses_consecutivos_con_deuda"] = (racha * con_problema).astype("int16")

    return df


# ---------------------------------------------------------------------------
# Features temporales
# ---------------------------------------------------------------------------


# Cuantas metricas se procesan por vez al calcular los lags. Cada bloque materializa
# `len(lags)` copias de sus columnas, asi que este numero fija el pico de memoria:
# 707k filas x 16 columnas x 3 lags x 4 bytes = ~135 MB por bloque, contra los varios GB
# que costaba traer las 82 metricas de una. Es lo que permite que el pipeline entre en
# un contenedor de 2 GB en vez de necesitar la memoria de una notebook grande.
LAG_CHUNK_SIZE = 16


def _add_temporal_features(
    df: pd.DataFrame,
    numeric_cols: list[str],
    lags: list[int],
    window: int,
) -> pd.DataFrame:
    """Deltas, ratios y pendientes contra los meses anteriores de la misma cuenta.

    Las metricas se procesan de a bloques: para cada uno se traen sus lags, se calculan
    las derivadas y se libera antes de seguir. Traerlas todas juntas multiplica el panel
    por cuatro y hace que el proceso muera por falta de memoria en cualquier contenedor
    de tamaño razonable.
    """
    core = set(CORE_USAGE_COLS + ["ventas_totales", "adiciones_totales"]) & set(df.columns)
    lags = sorted(set(lags))

    claves = df[["id", "t"]]
    derivadas: list[pd.DataFrame] = []

    for inicio in range(0, len(numeric_cols), LAG_CHUNK_SIZE):
        bloque = numeric_cols[inicio : inicio + LAG_CHUNK_SIZE]
        anteriores = _lagged_block(df, claves, bloque, lags)
        derivadas.append(_block_derivatives(df, anteriores, bloque, core, window))
        del anteriores

    merged = pd.concat([df, *derivadas], axis=1)
    del derivadas

    duplicated = merged.columns[merged.columns.duplicated()].tolist()
    if duplicated:
        raise RuntimeError(f"Columnas duplicadas en la matriz de features: {duplicated}")
    return merged


def _lagged_block(
    df: pd.DataFrame, claves: pd.DataFrame, bloque: list[str], lags: list[int]
) -> dict[int, pd.DataFrame]:
    """Valores de `bloque` en t-lag, alineados fila a fila con `df`.

    El join va sobre (id, t - lag) y no sobre la posicion: hay cuentas con huecos en el
    panel y un desplazamiento ciego tomaria el mes equivocado.
    """
    valores = df[["id", "t", *bloque]].astype({c: "float32" for c in bloque})
    salida: dict[int, pd.DataFrame] = {}

    for lag in lags:
        previo = valores.copy()
        previo["t"] = previo["t"] + lag
        unido = claves.merge(previo, on=["id", "t"], how="left", sort=False)
        if len(unido) != len(df):
            raise RuntimeError(
                f"El join de lag {lag} cambio la cantidad de filas "
                f"({len(df):,} -> {len(unido):,}): hay (id, periodo) duplicados."
            )
        salida[lag] = unido[bloque].set_axis(df.index)

    return salida


def _block_derivatives(
    df: pd.DataFrame,
    anteriores: dict[int, pd.DataFrame],
    bloque: list[str],
    core: set[str],
    window: int,
) -> pd.DataFrame:
    """Calcula las derivadas temporales de un bloque de metricas."""
    nuevas: dict[str, pd.Series] = {}
    ventana = [lag for lag in anteriores if lag <= window]

    for col in bloque:
        actual = df[col].astype("float32")

        if 1 in anteriores:
            nuevas[f"{col}_delta_1m"] = actual - anteriores[1][col]

        if ventana:
            # Promedio de los meses previos disponibles dentro de la ventana.
            base = pd.concat([anteriores[lag][col] for lag in ventana], axis=1).mean(axis=1)
            nuevas[f"{col}_ratio_3m"] = (
                (actual / base.replace(0, np.nan)).clip(0, 10).astype("float32")
            )

        if col in core and 3 in anteriores:
            nuevas[f"{col}_delta_3m"] = actual - anteriores[3][col]
            if 1 in anteriores and 2 in anteriores:
                # Pendiente de la recta que pasa por (t-3, t-2, t-1, t).
                nuevas[f"{col}_slope_3m"] = _slope(
                    [anteriores[3][col], anteriores[2][col], anteriores[1][col], actual]
                )

    # Algunas derivadas ya se calcularon a mano antes (estado_severity_delta_1m, por
    # ejemplo); las de aca no las pisan.
    nuevas = {k: v.astype("float32") for k, v in nuevas.items() if k not in df.columns}
    return pd.DataFrame(nuevas, index=df.index)


def _slope(series_list: list[pd.Series]) -> pd.Series:
    """Pendiente OLS de una secuencia corta y equiespaciada de valores."""
    n = len(series_list)
    x = np.arange(n, dtype="float32")
    x_mean = x.mean()
    denom = float(((x - x_mean) ** 2).sum())
    values = pd.concat(series_list, axis=1)
    y_mean = values.mean(axis=1)
    num = sum((x[i] - x_mean) * (series_list[i] - y_mean) for i in range(n))
    return (num / denom).astype("float32")


def _add_history_features(df: pd.DataFrame) -> pd.DataFrame:
    """Historia de la cuenta dentro del panel: antiguedad, pausas previas, cambios de plan."""
    grp = df.groupby("id", sort=False)

    # Meses de calendario transcurridos desde la primera aparicion en el panel.
    df["meses_desde_alta_en_panel"] = (df["t"] - grp["t"].transform("min")).astype("int16")

    # Diferencia entre meses transcurridos y meses observados: cuenta las pausas previas.
    if "months_in_panel" in df.columns:
        df["pausas_previas"] = (
            (df["meses_desde_alta_en_panel"] + 1 - df["months_in_panel"])
            .clip(lower=0)
            .astype("int16")
        )

    # Cambios de plan: un downgrade o la baja de un modulo suele preceder a la baja total.
    prev_rank = grp["plan_rank"].shift(1)
    prev_mods = grp["plan_n_modulos"].shift(1)
    prev_plan = grp["plan"].shift(1)

    df["plan_cambio_1m"] = (df["plan"].ne(prev_plan) & prev_plan.notna()).astype("int8")
    df["plan_rank_delta_1m"] = (df["plan_rank"] - prev_rank).fillna(0).astype("int8")
    df["plan_modulos_delta_1m"] = (df["plan_n_modulos"] - prev_mods).fillna(0).astype("int8")
    df["plan_downgrade_1m"] = (
        (df["plan_rank_delta_1m"] < 0) | (df["plan_modulos_delta_1m"] < 0)
    ).astype("int8")

    return df


def feature_columns(df: pd.DataFrame, exclude_patterns: list[str] | None = None) -> list[str]:
    """Columnas que efectivamente se le pasan al modelo.

    `exclude_patterns` permite entrenar variantes dejando afuera familias enteras de
    features. El caso concreto: las features de cobranza (`estado_*`, `*deuda*`) son las
    mas predictivas pero tambien las mas tardias — cuando una cuenta ya esta BLOCKED, la
    baja practicamente ya ocurrio. Excluirlas mide cuanta señal hay en el comportamiento
    de uso puro, que es lo que le da margen de accion al equipo de CX.
    """
    patterns = exclude_patterns or []
    cols = []
    for col in df.columns:
        if col in NON_FEATURE_COLS or col.startswith("__"):
            continue
        if any(pat in col for pat in patterns):
            continue
        if pd.api.types.is_numeric_dtype(df[col]) or isinstance(df[col].dtype, pd.CategoricalDtype):
            cols.append(col)
    return cols
