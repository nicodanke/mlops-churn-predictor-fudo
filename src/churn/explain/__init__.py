"""Explicabilidad por prediccion: que features empujaron el riesgo de cada cuenta."""

from churn.explain.narrative import build_narrative
from churn.explain.shap_explainer import FeatureContribution, ShapExplainer

__all__ = ["ShapExplainer", "FeatureContribution", "build_narrative"]
