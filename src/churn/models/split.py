"""Split temporal del panel.

Un split aleatorio filtraria informacion del futuro: la misma cuenta aparece en muchos
meses y sus features estan correlacionadas entre si. El unico split honesto es por
periodo: entrenar con meses viejos y evaluar con los mas recientes, que es exactamente
como se va a usar el modelo en produccion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class TemporalSplit:
    train: pd.DataFrame
    val: pd.DataFrame
    test: pd.DataFrame
    train_periods: list[int]
    val_periods: list[int]
    test_periods: list[int]

    def summary(self) -> pd.DataFrame:
        rows = []
        for name, frame, periods in (
            ("train", self.train, self.train_periods),
            ("val", self.val, self.val_periods),
            ("test", self.test, self.test_periods),
        ):
            rows.append(
                {
                    "split": name,
                    "periodos": f"{periods[0]} - {periods[-1]}" if periods else "-",
                    "n_periodos": len(periods),
                    "filas": len(frame),
                    "churn_rate_%": round(float(frame["churn"].mean()) * 100, 2)
                    if len(frame)
                    else 0.0,
                }
            )
        return pd.DataFrame(rows)


def temporal_split(
    df: pd.DataFrame,
    n_test_periods: int = 2,
    n_val_periods: int = 2,
    min_months_in_panel: int = 2,
    exclude_account_ids: set[int] | None = None,
) -> TemporalSplit:
    """Parte el dataset etiquetado en train / val / test por periodo calendario.

    `exclude_account_ids` saca cuentas enteras del entrenamiento — se usa para las que
    tienen estacionalidad de uso, cuyas bajas y altas recurrentes son ruido para el modelo.

    El filtrado se resuelve con mascaras booleanas y no materializando frames
    intermedios: cada `.copy()` sobre este panel son cientos de megabytes, y el
    encadenado de copias es lo que hacia que el pipeline no entrara en un contenedor.
    """
    elegible = df["churn"].notna()

    if min_months_in_panel > 1:
        suficiente_historia = df["months_in_panel"] >= min_months_in_panel
        logger.info(
            "Filtradas %s filas de cuentas con menos de %s meses de historia",
            f"{int((elegible & ~suficiente_historia).sum()):,}",
            min_months_in_panel,
        )
        elegible &= suficiente_historia

    excluida = (
        df["id"].isin(exclude_account_ids)
        if exclude_account_ids
        else pd.Series(False, index=df.index)
    )

    periods = sorted(df.loc[elegible, "periodo"].unique())
    if len(periods) <= n_test_periods + n_val_periods:
        raise ValueError(
            f"Solo hay {len(periods)} periodos etiquetables; no alcanzan para un split "
            f"de {n_val_periods} de validacion + {n_test_periods} de test."
        )

    test_periods = periods[-n_test_periods:]
    val_periods = periods[-(n_test_periods + n_val_periods) : -n_test_periods]
    train_periods = periods[: -(n_test_periods + n_val_periods)]

    # Las cuentas estacionales se sacan de train y val, pero NO de test.
    #
    # Sacarlas de train evita que el modelo aprenda que "cerrar en mayo" es churn, y
    # sacarlas de val evita que deformen la calibracion y el umbral. Pero en produccion
    # esas cuentas se scorean igual — el batch corre sobre toda la base activa — asi que
    # evaluarlas fuera del test daria una metrica mas linda que la realidad.
    entrenable = elegible & ~excluida
    if exclude_account_ids:
        logger.info(
            "Excluidas de train/val %s filas de %s cuentas con estacionalidad de uso "
            "(se mantienen en test: en produccion se scorean igual)",
            f"{int((elegible & excluida).sum()):,}",
            f"{len(exclude_account_ids):,}",
        )

    def parte(mascara: pd.Series, periodos: list[int]) -> pd.DataFrame:
        seleccion = df[mascara & df["periodo"].isin(periodos)]
        return seleccion.assign(churn=seleccion["churn"].astype("int8"))

    split = TemporalSplit(
        train=parte(entrenable, train_periods),
        val=parte(entrenable, val_periods),
        test=parte(elegible, test_periods),
        train_periods=train_periods,
        val_periods=val_periods,
        test_periods=test_periods,
    )
    logger.info("Split temporal:\n%s", split.summary().to_string(index=False))
    return split
