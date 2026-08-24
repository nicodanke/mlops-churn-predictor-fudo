"""Parseo de los plan codes de Fudo.

Un plan code es una lista de tokens separados por guion, por ejemplo `adv-tbl-mx`:

    adv   -> plan base (avanzado)
    tbl   -> modulo contratado (mesas)
    mx    -> mercado / pais de facturacion (Mexico)

El ultimo token es siempre el mercado, el primero el plan base y todos los del medio
son modulos. `int` en la posicion de mercado significa "internacional" (cuentas fuera
de los mercados con lista de precios propia).
"""

from __future__ import annotations

from dataclasses import dataclass

# Mercados con lista de precios propia. El sufijo del plan code siempre es uno de estos.
MARKETS: tuple[str, ...] = ("ar", "cl", "co", "br", "mx", "uy", "int")

# Planes base observados en el data warehouse.
BASE_PLANS: tuple[str, ...] = ("ini", "ba", "st", "adv", "pro", "fu", "pr")

# Modulos adicionales que se suman al plan base.
MODULES: tuple[str, ...] = ("tbl", "dv", "idvsls", "kds", "fi", "fe", "sbw", "top")

# Etiquetas legibles para la UI y para los textos de explicabilidad.
BASE_PLAN_LABELS: dict[str, str] = {
    "ini": "Inicial",
    "ba": "Basico",
    "st": "Standard",
    "adv": "Avanzado",
    "pro": "Pro",
    "fu": "Full",
    "pr": "Pro (codigo legacy)",
}

MODULE_LABELS: dict[str, str] = {
    "tbl": "Mesas",
    "dv": "Delivery",
    "idvsls": "Ventas individuales",
    "kds": "Kitchen Display System",
    "fi": "Facturacion electronica",
    "fe": "Factura electronica (fe)",
    "sbw": "Subway / integracion",
    "top": "Top / add-on premium",
}

MARKET_LABELS: dict[str, str] = {
    "ar": "Argentina",
    "cl": "Chile",
    "co": "Colombia",
    "br": "Brasil",
    "mx": "Mexico",
    "uy": "Uruguay",
    "int": "Internacional",
}

# Orden comercial de los planes base (sirve para detectar upgrades y downgrades).
BASE_PLAN_RANK: dict[str, int] = {"ini": 1, "ba": 2, "st": 3, "adv": 4, "pr": 5, "pro": 5, "fu": 6}


@dataclass(frozen=True)
class ParsedPlan:
    """Resultado de descomponer un plan code."""

    code: str
    base: str | None
    modules: tuple[str, ...]
    market: str | None
    unknown_tokens: tuple[str, ...] = ()

    @property
    def n_modules(self) -> int:
        return len(self.modules)

    @property
    def rank(self) -> int:
        """Nivel comercial del plan base (0 si no se reconoce)."""
        return BASE_PLAN_RANK.get(self.base or "", 0)

    def describe(self) -> str:
        base = BASE_PLAN_LABELS.get(self.base or "", self.base or "?")
        market = MARKET_LABELS.get(self.market or "", self.market or "?")
        if not self.modules:
            return f"Plan {base} ({market})"
        mods = ", ".join(MODULE_LABELS.get(m, m) for m in self.modules)
        return f"Plan {base} + {mods} ({market})"


def parse_plan(code: str | None) -> ParsedPlan:
    """Descompone un plan code en plan base, modulos y mercado.

    Es tolerante: tokens desconocidos se acumulan en `unknown_tokens` en vez de
    romper, para que la aparicion de un modulo nuevo no tire abajo el pipeline.
    """
    if not code or not isinstance(code, str):
        return ParsedPlan(code=str(code), base=None, modules=(), market=None)

    tokens = [t for t in code.strip().lower().split("-") if t]
    if not tokens:
        return ParsedPlan(code=code, base=None, modules=(), market=None)

    market = tokens[-1] if tokens[-1] in MARKETS else None
    body = tokens[:-1] if market else tokens

    base = body[0] if body and body[0] in BASE_PLANS else None
    rest = body[1:] if base else body

    modules = tuple(t for t in rest if t in MODULES)
    unknown = tuple(t for t in rest if t not in MODULES)

    return ParsedPlan(
        code=code,
        base=base,
        modules=modules,
        market=market,
        unknown_tokens=unknown,
    )
