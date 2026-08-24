"""Lectura del snapshot mensual y armado del panel cuenta x periodo."""

from __future__ import annotations

import logging
from glob import glob
from pathlib import Path

import pandas as pd

from churn.data.schema import ESTADO_SEVERITY, normalize_column

logger = logging.getLogger(__name__)


def load_raw(path: str | Path) -> pd.DataFrame:
    """Lee el export del data warehouse y normaliza los headers.

    Acepta tres formas, porque el DW puede entregar el historico completo o un archivo
    por mes:

        data/account_stats.csv     un archivo puntual
        data/                      todos los .csv y .parquet del directorio
        data/stats_*.csv           un patron

    Cuando son varios archivos se concatenan y, si un (cuenta, periodo) viene repetido,
    gana el del archivo que ordena ultimo por nombre — que con nombres con fecha es el
    mas reciente. Eso permite reemplazar un mes ya cargado sin tener que borrar nada.
    """
    fuentes = resolve_sources(path)

    if len(fuentes) == 1:
        logger.info("Leyendo snapshot desde %s", fuentes[0])
        df = _read_one(fuentes[0])
    else:
        logger.info("Leyendo %s archivos de snapshot desde %s", len(fuentes), path)
        partes = []
        for fuente in fuentes:
            parte = _read_one(fuente)
            periodos = sorted(parte["periodo"].unique()) if "periodo" in parte else []
            logger.info(
                "  %s: %s filas%s",
                Path(fuente).name,
                f"{len(parte):,}",
                f", periodos {periodos[0]}-{periodos[-1]}" if periodos else "",
            )
            partes.append(parte)

        _warn_on_schema_drift(fuentes, partes)
        df = pd.concat(partes, ignore_index=True, sort=False)
        del partes

        if {"id", "periodo"} <= set(df.columns):
            antes = len(df)
            df = df.drop_duplicates(["id", "periodo"], keep="last")
            if antes != len(df):
                logger.info(
                    "  %s filas repetidas entre archivos; se conserva la del ultimo",
                    f"{antes - len(df):,}",
                )

    df = _downcast(df)
    logger.info("Snapshot leido: %s filas x %s columnas", f"{len(df):,}", df.shape[1])
    return df


def resolve_sources(path: str | Path) -> list[str]:
    """Expande un directorio o un patron a la lista concreta de archivos a leer."""
    src = str(path)

    # Las rutas remotas se pasan tal cual: pandas y gcsfs resuelven gs:// por su cuenta.
    if "://" in src:
        return [src]

    candidato = Path(src)
    if candidato.is_dir():
        encontrados = sorted(
            f for ext in ("*.csv", "*.csv.gz", "*.parquet") for f in candidato.glob(ext)
        )
    elif any(c in src for c in "*?["):
        encontrados = sorted(Path(p) for p in glob(src))
    else:
        encontrados = [candidato] if candidato.exists() else []

    if not encontrados:
        raise FileNotFoundError(
            f"No hay ningun archivo de snapshot en '{src}'. "
            "Ver data/README.md para saber que archivo hace falta y de donde sacarlo."
        )
    return [str(f) for f in encontrados]


def _read_one(src: str) -> pd.DataFrame:
    df = pd.read_parquet(src) if src.endswith(".parquet") else pd.read_csv(src, low_memory=False)
    df.columns = [normalize_column(c) for c in df.columns]
    return df


def _warn_on_schema_drift(fuentes: list[str], partes: list[pd.DataFrame]) -> None:
    """Avisa si algun archivo trae columnas distintas.

    Cuando el DW agrega una metrica nueva, los meses viejos no la tienen y quedan en NaN.
    El modelo lo tolera (XGBoost maneja los faltantes), pero conviene saberlo en vez de
    descubrirlo por una feature que aparece medio vacia.
    """
    referencia = set(partes[0].columns)
    for fuente, parte in zip(fuentes[1:], partes[1:], strict=True):
        faltan = referencia - set(parte.columns)
        sobran = set(parte.columns) - referencia
        if faltan or sobran:
            logger.warning(
                "  %s tiene un esquema distinto al primer archivo: %s%s",
                Path(fuente).name,
                f"le faltan {sorted(faltan)}" if faltan else "",
                f" trae de mas {sorted(sobran)}" if sobran else "",
            )


def _downcast(df: pd.DataFrame) -> pd.DataFrame:
    """Baja los numericos a 32 bits.

    Todas las metricas del snapshot son conteos de operaciones de un mes: el maximo del
    dataset esta en el orden de las decenas de miles, muy lejos del limite de int32
    (2.100 millones) y dentro del rango donde float32 representa enteros de forma exacta
    (16,7 millones). Pandas lee todo en 64 bits por defecto, lo que duplica el panel sin
    ganar nada.

    Con 707 mil filas y ~300 columnas la diferencia son gigabytes: es lo que decide si
    el pipeline entra en un contenedor chico o muere por falta de memoria.
    """
    conversiones = {}
    for col in df.columns:
        tipo = df[col].dtype
        if col in ("id", "internal_pk", "periodo"):
            continue
        if pd.api.types.is_integer_dtype(tipo):
            conversiones[col] = "int32"
        elif pd.api.types.is_float_dtype(tipo):
            conversiones[col] = "float32"

    return df.astype(conversiones) if conversiones else df


def load_panel(path: str | Path) -> pd.DataFrame:
    """Devuelve el panel ordenado por (cuenta, periodo) con las columnas base derivadas.

    El panel es la unidad de trabajo del pipeline: una fila = una cuenta en un mes.
    Agrega el indice de periodo `t` (0..n-1), que es lo que permite razonar sobre
    "el mes siguiente" sin pelear con el formato YYYYMM.
    """
    df = load_raw(path)

    required = {"id", "periodo"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"El snapshot no tiene las columnas requeridas: {sorted(missing)}")

    df["periodo"] = df["periodo"].astype(int)

    dupes = df.duplicated(["id", "periodo"]).sum()
    if dupes:
        logger.warning("Hay %s filas duplicadas por (id, periodo); se conserva la ultima", dupes)
        df = df.drop_duplicates(["id", "periodo"], keep="last")

    # Indice de periodo denso: 202401 -> 0, 202402 -> 1, ...
    periods = sorted(df["periodo"].unique())
    period_index = {p: i for i, p in enumerate(periods)}
    df["t"] = df["periodo"].map(period_index).astype("int16")

    # Fecha de alta de la cuenta -> antiguedad en meses al momento del snapshot.
    if "fecha" in df.columns:
        created = pd.to_datetime(df["fecha"], errors="coerce", utc=True)
        snapshot = pd.to_datetime(df["periodo"].astype(str) + "01", format="%Y%m%d", utc=True)
        tenure = (snapshot - created).dt.days / 30.44
        # 1970-01-01 es un placeholder de "fecha desconocida" en el DW, no una cuenta de 50 años.
        df["tenure_months"] = tenure.where((tenure >= 0) & (tenure < 400)).astype("float32")
    else:
        df["tenure_months"] = pd.NA

    if "estado" in df.columns:
        df["estado_severity"] = df["estado"].map(ESTADO_SEVERITY).fillna(0).astype("int8")

    df = df.sort_values(["id", "t"], kind="mergesort").reset_index(drop=True)
    logger.info(
        "Panel armado: %s cuentas, %s periodos (%s a %s)",
        f"{df['id'].nunique():,}",
        len(periods),
        periods[0],
        periods[-1],
    )
    return df


def period_labels(df: pd.DataFrame) -> dict[int, int]:
    """Mapa indice de periodo -> periodo YYYYMM."""
    pairs = df[["t", "periodo"]].drop_duplicates().sort_values("t")
    return dict(zip(pairs["t"].astype(int), pairs["periodo"].astype(int), strict=True))


def next_period(periodo: int) -> int:
    """202412 -> 202501."""
    year, month = divmod(int(periodo), 100)
    return year * 100 + month + 1 if month < 12 else (year + 1) * 100 + 1
