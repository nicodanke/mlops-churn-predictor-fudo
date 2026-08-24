"""Calculo del revenue mensual de una cuenta a partir de su plan code.

El revenue NO entra al modelo: el modelo estima unicamente la probabilidad de churn.
Se usa despues, para convertir esa probabilidad en riesgo economico:

    revenue_en_riesgo = probabilidad_de_churn * revenue_mensual

Los precios viven en `config/pricing.yaml` y se cargan por (item, mercado).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from churn.config import DEFAULT_PRICING_PATH
from churn.pricing.plans import ParsedPlan, parse_plan


@dataclass
class PricingBook:
    """Lista de precios por plan base y modulo, desglosada por mercado."""

    currency: str
    plans: dict[str, dict[str, float | None]]
    modules: dict[str, dict[str, float | None]]
    default_market: str = "int"

    @classmethod
    def load(cls, path: str | Path | None = None) -> PricingBook:
        p = Path(path or DEFAULT_PRICING_PATH)
        if not p.exists():
            raise FileNotFoundError(
                f"No existe la lista de precios en {p}. "
                "Genera la plantilla con `make pricing-template` y completa los valores."
            )
        with open(p) as fh:
            data = yaml.safe_load(fh) or {}
        return cls(
            currency=data.get("currency", "USD"),
            plans=data.get("plans", {}) or {},
            modules=data.get("modules", {}) or {},
            default_market=data.get("default_market", "int"),
        )

    def price(self, kind: str, item: str, market: str | None) -> float | None:
        """Precio de un plan base o modulo en un mercado. None si esta sin cargar."""
        table = self.plans if kind == "plan" else self.modules
        entry = table.get(item)
        if not entry:
            return None
        value = entry.get(market or self.default_market)
        if value is None:
            value = entry.get(self.default_market)
        return None if value is None else float(value)

    @property
    def is_filled(self) -> bool:
        """True si al menos un precio fue cargado (distinto de null y de 0)."""
        for table in (self.plans, self.modules):
            for entry in table.values():
                for value in entry.values():
                    if value:
                        return True
        return False


@dataclass
class RevenueBreakdown:
    """Desglose del revenue mensual, util para mostrar en la UI."""

    total: float
    currency: str
    market: str | None
    items: list[dict[str, object]]
    missing_prices: list[str]

    @property
    def is_complete(self) -> bool:
        return not self.missing_prices


def revenue_for_plan(
    plan_code: str | None,
    book: PricingBook,
    parsed: ParsedPlan | None = None,
) -> RevenueBreakdown:
    """Suma el precio del plan base mas el de cada modulo, en el mercado de la cuenta."""
    parsed = parsed or parse_plan(plan_code)
    items: list[dict[str, object]] = []
    missing: list[str] = []
    total = 0.0

    if parsed.base:
        value = book.price("plan", parsed.base, parsed.market)
        if value is None:
            missing.append(f"plan:{parsed.base}@{parsed.market}")
        else:
            total += value
        items.append({"kind": "plan", "code": parsed.base, "price": value})

    for module in parsed.modules:
        value = book.price("module", module, parsed.market)
        if value is None:
            missing.append(f"module:{module}@{parsed.market}")
        else:
            total += value
        items.append({"kind": "module", "code": module, "price": value})

    return RevenueBreakdown(
        total=round(total, 2),
        currency=book.currency,
        market=parsed.market,
        items=items,
        missing_prices=missing,
    )
