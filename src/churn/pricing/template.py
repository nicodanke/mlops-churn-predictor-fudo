"""Generacion de la plantilla de precios a partir de los planes vistos en los datos.

Escanea el snapshot, descompone todos los plan codes y arma un `pricing.yaml` con una
entrada por (plan base, mercado) y (modulo, mercado), con los precios en null para que
se completen a mano con la lista de precios real de Fudo en cada pais.

Si ya existe un pricing.yaml, se conservan los valores cargados y solo se agregan las
combinaciones nuevas.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yaml

from churn.pricing.plans import (
    BASE_PLAN_LABELS,
    MARKET_LABELS,
    MODULE_LABELS,
    parse_plan,
)

logger = logging.getLogger(__name__)

HEADER = """\
# ============================================================================
#  Lista de precios de Fudo por plan y modulo, desglosada por mercado.
#
#  COMPLETAR A MANO. Cada valor es el precio mensual, en la moneda declarada en
#  `currency`, que la cuenta paga por ese item en ese mercado. El revenue mensual
#  de una cuenta es la suma del precio de su plan base mas el de cada modulo:
#
#      adv-tbl-mx  ->  plans.adv.mx  +  modules.tbl.mx
#
#  Los valores en `null` significan "sin cargar": esas cuentas van a quedar con
#  revenue 0 y el riesgo economico no se va a poder calcular bien para ellas.
#
#  Regenerar con `make pricing-template` (conserva los valores ya cargados).
# ============================================================================

"""


def build_pricing_template(
    plan_codes: pd.Series | list[str],
    output_path: str | Path,
    currency: str = "USD",
) -> Path:
    """Escribe (o actualiza) config/pricing.yaml con todos los items observados."""
    path = Path(output_path)
    existing = _load_existing(path)

    codes = pd.Series(list(plan_codes)).dropna().astype(str).unique()
    parsed = [parse_plan(c) for c in codes]

    counts = pd.Series([c for c in plan_codes]).value_counts()

    plans: dict[str, dict[str, float | None]] = {}
    modules: dict[str, dict[str, float | None]] = {}
    unknown: set[str] = set()

    for p in parsed:
        market = p.market or "int"
        if p.base:
            plans.setdefault(p.base, {}).setdefault(market, None)
        for module in p.modules:
            modules.setdefault(module, {}).setdefault(market, None)
        unknown.update(p.unknown_tokens)

    # Preservar lo ya cargado a mano.
    for table, previous in (
        (plans, existing.get("plans", {})),
        (modules, existing.get("modules", {})),
    ):
        for item, markets in previous.items():
            for market, value in (markets or {}).items():
                table.setdefault(item, {})[market] = value

    plans = {k: dict(sorted(v.items())) for k, v in sorted(plans.items())}
    modules = {k: dict(sorted(v.items())) for k, v in sorted(modules.items())}

    if unknown:
        logger.warning(
            "Tokens de plan no reconocidos (revisar churn/pricing/plans.py): %s",
            ", ".join(sorted(unknown)),
        )

    doc = {
        "currency": existing.get("currency", currency),
        "default_market": existing.get("default_market", "int"),
        "plans": plans,
        "modules": modules,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(HEADER + _annotate(body, counts))

    n_missing = sum(
        1
        for table in (plans, modules)
        for markets in table.values()
        for v in markets.values()
        if v is None
    )
    logger.info(
        "Plantilla escrita en %s: %s planes base, %s modulos, %s precios sin cargar",
        path,
        len(plans),
        len(modules),
        n_missing,
    )
    return path


def _annotate(body: str, counts: pd.Series) -> str:
    """Agrega el nombre legible de cada plan/modulo como comentario al lado."""
    labels = {**BASE_PLAN_LABELS, **MODULE_LABELS, **MARKET_LABELS}
    out = []
    for line in body.splitlines():
        stripped = line.strip().rstrip(":")
        indent = len(line) - len(line.lstrip())
        if indent == 2 and stripped in labels and line.rstrip().endswith(":"):
            out.append(f"{line}  # {labels[stripped]}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _load_existing(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def pricing_coverage(plan_codes: pd.Series, book) -> pd.DataFrame:
    """Cuantas cuentas quedan sin precio, para saber cuanto falta completar."""
    from churn.pricing.revenue import revenue_for_plan

    counts = plan_codes.value_counts()
    rows = []
    for code, n in counts.items():
        breakdown = revenue_for_plan(str(code), book)
        rows.append(
            {
                "plan": code,
                "cuentas": int(n),
                "revenue_mensual": breakdown.total,
                "completo": breakdown.is_complete,
                "faltantes": ", ".join(breakdown.missing_prices),
            }
        )
    return pd.DataFrame(rows)
