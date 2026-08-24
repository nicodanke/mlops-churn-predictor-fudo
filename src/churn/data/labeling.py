"""Inferencia de la etiqueta de churn a partir de la presencia en el snapshot.

El reporte mensual de Fudata solo incluye cuentas con estado comercial ACTIVE al momento
de generarse. Que una cuenta deje de aparecer significa, entonces, que dejo de estar
activa: ahi esta la señal de churn, y hay que derivarla porque no viene como columna.

La sutileza es que ~17% de las desapariciones son temporales: la cuenta vuelve uno o dos
meses despues. Son restaurantes estacionales o pausas, no bajas. Si se etiquetan como
churn el modelo aprende ruido, asi que se exige una ventana de confirmacion: una cuenta
churnea solo si no reaparece en los siguientes `confirm_window` meses.

Estados posibles de la etiqueta para la fila (cuenta, t):

    churn = 1   presente en t, ausente en t+1 .. t+W
    churn = 0   presente en t y en t+1
    ambiguo     ausente en t+1 pero reaparece dentro de la ventana (pausa)
    no evaluable  no hay suficiente futuro observado para decidir (ultimos W meses)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

LABEL_CHURN = 1
LABEL_ACTIVE = 0


def build_labels(
    panel: pd.DataFrame,
    confirm_window: int = 3,
    ambiguous_policy: str = "drop",
) -> pd.DataFrame:
    """Agrega las columnas de etiquetado al panel.

    Columnas resultantes:
        churn           1 / 0 / NaN (NaN = no evaluable o ambigua descartada)
        is_ambiguous    la cuenta se ausento pero volvio dentro de la ventana
        is_labelable    hay futuro suficiente observado para decidir la etiqueta
        months_in_panel cuantos meses lleva la cuenta apareciendo en el panel
    """
    if ambiguous_policy not in {"drop", "negative"}:
        raise ValueError(f"ambiguous_policy invalido: {ambiguous_policy!r}")

    df = panel.sort_values(["id", "t"], kind="mergesort").reset_index(drop=True)
    max_t = int(df["t"].max())

    # Matriz de presencia dispersa: set de pares (cuenta, periodo) observados.
    present = set(zip(df["id"].to_numpy(), df["t"].to_numpy(), strict=True))

    ids = df["id"].to_numpy()
    ts = df["t"].to_numpy()

    absent_next = np.fromiter(
        ((i, t + 1) not in present for i, t in zip(ids, ts, strict=True)),
        dtype=bool,
        count=len(df),
    )

    # Reaparece en algun momento de t+2 .. t+W: la ausencia fue una pausa.
    returns_in_window = np.zeros(len(df), dtype=bool)
    for offset in range(2, confirm_window + 1):
        returns_in_window |= np.fromiter(
            ((i, t + offset) in present for i, t in zip(ids, ts, strict=True)),
            dtype=bool,
            count=len(df),
        )

    # Solo se puede confirmar una baja si toda la ventana cae dentro de lo observado.
    labelable = ts + confirm_window <= max_t

    is_ambiguous = absent_next & returns_in_window
    churn = np.where(absent_next & ~returns_in_window, LABEL_CHURN, LABEL_ACTIVE).astype("float32")
    churn[~labelable] = np.nan
    if ambiguous_policy == "drop":
        churn[is_ambiguous] = np.nan

    df["churn"] = churn
    df["is_ambiguous"] = is_ambiguous
    df["is_labelable"] = labelable
    df["months_in_panel"] = df.groupby("id").cumcount().astype("int16") + 1

    _log_summary(df, confirm_window, ambiguous_policy)
    return df


def _log_summary(df: pd.DataFrame, window: int, policy: str) -> None:
    labeled = df["churn"].notna()
    n_labeled = int(labeled.sum())
    rate = float(df.loc[labeled, "churn"].mean()) if n_labeled else 0.0
    logger.info(
        "Etiquetado (ventana=%s meses, ambiguas=%s): %s filas etiquetadas de %s "
        "| churn rate %.2f%% | ambiguas %s (%.1f%%)",
        window,
        policy,
        f"{n_labeled:,}",
        f"{len(df):,}",
        rate * 100,
        f"{int(df['is_ambiguous'].sum()):,}",
        float(df["is_ambiguous"].mean()) * 100,
    )


def churn_rate_by_period(df: pd.DataFrame) -> pd.DataFrame:
    """Churn rate mensual sobre las filas etiquetadas. Sirve como baseline de negocio."""
    labeled = df[df["churn"].notna()]
    out = labeled.groupby("periodo")["churn"].agg(cuentas="size", churn_rate="mean")
    out["churn_rate"] = (out["churn_rate"] * 100).round(2)
    return out.reset_index()
