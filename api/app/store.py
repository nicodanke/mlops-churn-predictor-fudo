"""Acceso a los resultados del scoring batch.

La API no ejecuta el modelo: lee lo que dejo el job batch. El parquet de un mes son
~32k filas, asi que entra comodo en memoria y las consultas se resuelven con pandas
sin necesidad de una base de datos. Cuando el volumen lo pida, este modulo es el unico
lugar que hay que cambiar para apuntar a BigQuery o Firestore.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class PredictionStore:
    """Indexa los batches disponibles en disco y los cachea a demanda."""

    def __init__(self, predictions_dir: str | Path):
        self.root = Path(predictions_dir)
        self._cache: dict[int, pd.DataFrame] = {}
        self._meta_cache: dict[int, dict[str, Any]] = {}
        # Fecha de modificacion del archivo con la que se lleno cada entrada del cache.
        self._stamps: dict[int, float] = {}

    def available_periods(self) -> list[int]:
        """Periodos con un batch escrito, del mas nuevo al mas viejo."""
        if not self.root.exists():
            return []
        periods = []
        for child in self.root.iterdir():
            if child.is_dir() and child.name.isdigit() and (child / "predictions.parquet").exists():
                periods.append(int(child.name))
        return sorted(periods, reverse=True)

    def latest_period(self) -> int | None:
        periods = self.available_periods()
        return periods[0] if periods else None

    def resolve(self, periodo: int | None) -> int:
        target = periodo or self.latest_period()
        if target is None:
            raise FileNotFoundError(
                f"No hay predicciones en {self.root}. Corre `make score` para generarlas."
            )
        if target not in self.available_periods():
            raise KeyError(f"No hay predicciones para el periodo {target}")
        return target

    def load(self, periodo: int | None = None) -> pd.DataFrame:
        target = self.resolve(periodo)
        path = self.root / str(target) / "predictions.parquet"

        if self._is_stale(target, path):
            df = pd.read_parquet(path)
            df["top_features"] = df["top_features"].map(_parse_json)
            self._cache[target] = df
            self._meta_cache.pop(target, None)
            self._stamps[target] = path.stat().st_mtime
            logger.info("Batch %s cargado en memoria: %s cuentas", target, f"{len(df):,}")

        return self._cache[target]

    def metadata(self, periodo: int | None = None) -> dict[str, Any]:
        target = self.resolve(periodo)
        if target not in self._meta_cache:
            self.load(target)  # revalida el cache si el batch se regenero
            with open(self.root / str(target) / "metadata.json") as fh:
                self._meta_cache[target] = json.load(fh)
        return self._meta_cache[target]

    def _is_stale(self, periodo: int, path: Path) -> bool:
        """True si hay que releer el batch del disco.

        Volver a correr el scoring reescribe el parquet del mismo periodo — pasa cada vez
        que se completan precios y se regeneran las predicciones. Sin esta comprobacion la
        API seguiria sirviendo la version vieja hasta que alguien la reinicie, que es una
        trampa dificil de ver: los numeros simplemente no cambian.
        """
        if periodo not in self._cache:
            return True
        try:
            return path.stat().st_mtime > self._stamps.get(periodo, 0)
        except OSError:
            return False  # el archivo desaparecio: mejor servir lo que hay en memoria

    def invalidate(self) -> None:
        """Descarta el cache entero. El refresco por fecha de archivo ya cubre el caso
        habitual; esto es para forzarlo a mano."""
        self._cache.clear()
        self._meta_cache.clear()
        self._stamps.clear()


def _parse_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def query_accounts(
    df: pd.DataFrame,
    risk: list[str] | None = None,
    search: str | None = None,
    pais: str | None = None,
    min_probability: float | None = None,
    seasonal: bool | None = None,
    sort_by: str = "revenue_at_risk",
    ascending: bool = False,
    page: int = 1,
    size: int = 50,
) -> tuple[pd.DataFrame, int]:
    """Filtra, ordena y pagina el listado de cuentas de un batch."""
    out = df

    if risk:
        out = out[out["risk_category"].isin(risk)]
    if pais:
        out = out[out["pais"].astype(str).str.casefold() == pais.casefold()]
    if min_probability is not None:
        out = out[out["churn_probability"] >= min_probability]
    if seasonal is not None and "posible_estacional" in out.columns:
        out = out[out["posible_estacional"].astype(bool) == seasonal]
    if search:
        needle = search.strip().casefold()
        by_name = (
            out["nombre"].astype(str).str.casefold().str.contains(needle, na=False, regex=False)
        )
        by_id = out["id"].astype(str) == needle
        out = out[by_name | by_id]

    if sort_by in out.columns:
        out = out.sort_values(sort_by, ascending=ascending, kind="mergesort")

    total = len(out)
    start = max(page - 1, 0) * size
    return out.iloc[start : start + size], total
