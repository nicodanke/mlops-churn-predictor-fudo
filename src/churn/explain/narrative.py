"""Generacion del texto que lee el equipo de CX debajo de cada prediccion.

Hay dos modos:

  - `rules` (por defecto): arma el texto a partir de las contribuciones SHAP con
    plantillas. Es determinista, gratis, no necesita red y no puede alucinar. Es lo que
    corre en el batch mensual sobre las ~32k cuentas.

  - `llm`: le pasa las mismas contribuciones a Claude para que redacte un parrafo mas
    natural y sugiera una accion concreta. Pensado para las cuentas de riesgo alto, que
    son pocas, donde el costo por cuenta se justifica.

En los dos casos el insumo son las contribuciones SHAP: el texto nunca inventa una causa
que el modelo no haya usado.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from churn.explain.shap_explainer import FeatureContribution

logger = logging.getLogger(__name__)

RISK_INTRO = {
    "alto": "Riesgo alto de baja",
    "medio": "Riesgo medio de baja",
    "bajo": "Riesgo bajo de baja",
    "no_churn": "Sin señales relevantes de baja",
}


def build_narrative(
    contributions: list[FeatureContribution],
    probability: float,
    risk_category: str,
    revenue: float | None = None,
    currency: str = "USD",
    max_factors: int = 3,
) -> str:
    """Resumen en lenguaje natural de por que la cuenta esta en esta categoria."""
    intro = RISK_INTRO.get(risk_category, "Riesgo de baja")
    parts = [f"{intro}: probabilidad estimada de {probability:.0%}."]

    if revenue:
        parts.append(
            f"Representa {revenue:,.0f} {currency}/mes, "
            f"con {probability * revenue:,.0f} {currency}/mes en riesgo."
        )

    pushing = [c for c in contributions if c.direction == "aumenta"][:max_factors]
    holding = [c for c in contributions if c.direction == "reduce"][:2]

    if pushing:
        factors = "; ".join(_phrase(c) for c in pushing)
        parts.append(f"Lo que mas empuja el riesgo: {factors}.")

    if holding and risk_category != "no_churn":
        factors = "; ".join(_phrase(c) for c in holding)
        parts.append(f"En contra del riesgo juega: {factors}.")

    return " ".join(parts)


def _phrase(contribution: FeatureContribution) -> str:
    """Frase corta para una contribucion, con el percentil si esta disponible."""
    # Solo se baja la primera letra: `.lower()` a secas rompe siglas como ACTIVE, QR o SII.
    label = contribution.label[:1].lower() + contribution.label[1:]
    if contribution.percentile is None:
        return label
    pct = contribution.percentile
    if pct <= 10:
        posicion = "muy por debajo del resto de la base"
    elif pct <= 30:
        posicion = "por debajo del resto de la base"
    elif pct >= 90:
        posicion = "muy por encima del resto de la base"
    elif pct >= 70:
        posicion = "por encima del resto de la base"
    else:
        posicion = "en linea con el resto de la base"
    return f"{label} ({posicion}, percentil {pct:.0f})"


def build_narrative_llm(
    contributions: list[FeatureContribution],
    probability: float,
    risk_category: str,
    account_name: str | None = None,
    revenue: float | None = None,
    currency: str = "USD",
    model: str = "claude-sonnet-5",
) -> str:
    """Version redactada por Claude. Cae al modo `rules` si no hay API key o falla la llamada.

    Requiere ANTHROPIC_API_KEY y el extra `llm` (`poetry install --extras llm`).
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.debug("Sin ANTHROPIC_API_KEY; se usa la narrativa por reglas")
        return build_narrative(contributions, probability, risk_category, revenue, currency)

    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("El extra 'llm' no esta instalado; se usa la narrativa por reglas")
        return build_narrative(contributions, probability, risk_category, revenue, currency)

    factores = "\n".join(
        f"- {c.label}: valor {c.value}, percentil {c.percentile}, "
        f"{c.direction} el riesgo (SHAP {c.shap_value:+.3f})"
        for c in contributions
    )
    prompt = f"""Sos analista de Customer Experience en Fudo, un SaaS de gestion para restaurantes.
Escribi para un compañero del equipo un diagnostico breve (maximo 4 oraciones,
en español rioplatense, sin markdown) sobre esta cuenta.

Cuenta: {account_name or "sin nombre"}
Probabilidad de baja el proximo mes: {probability:.1%}
Categoria de riesgo: {risk_category}
Revenue mensual: {revenue or 0:,.0f} {currency}

Factores que el modelo uso para esta cuenta (SHAP positivo = empuja hacia la baja):
{factores}

Explica que le esta pasando a la cuenta en terminos de negocio y cerra con una accion
concreta y accionable para retenerla. No inventes datos que no esten en la lista de factores."""

    try:
        client = Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:  # la narrativa nunca debe tumbar el batch
        logger.warning("Fallo la generacion con LLM (%s); se usa la narrativa por reglas", exc)
        return build_narrative(contributions, probability, risk_category, revenue, currency)
