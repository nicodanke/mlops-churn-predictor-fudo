"""Metricas de evaluacion offline.

La metrica de decision acordada con negocio es PR-AUC (average precision): con un churn
base de ~4%, el accuracy y el ROC-AUC son enganosos y la precision/recall sobre la clase
positiva es lo unico que refleja si el equipo de CX va a perder el tiempo o no.

Ademas se reportan precision@k y recall@k, que es como CX realmente va a usar esto:
"dame las 500 cuentas mas riesgosas de este mes".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)


@dataclass
class EvaluationResult:
    pr_auc: float
    roc_auc: float
    baseline_rate: float
    lift_over_baseline: float
    brier: float
    best_threshold: float
    best_f1: float
    precision_at_best: float
    recall_at_best: float
    top_k: list[dict[str, Any]] = field(default_factory=list)
    n_samples: int = 0
    n_positives: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_auc": round(self.pr_auc, 4),
            "roc_auc": round(self.roc_auc, 4),
            "baseline_rate": round(self.baseline_rate, 4),
            "lift_over_baseline": round(self.lift_over_baseline, 2),
            "brier": round(self.brier, 5),
            "best_threshold": round(self.best_threshold, 4),
            "best_f1": round(self.best_f1, 4),
            "precision_at_best": round(self.precision_at_best, 4),
            "recall_at_best": round(self.recall_at_best, 4),
            "n_samples": self.n_samples,
            "n_positives": self.n_positives,
            "top_k": self.top_k,
        }

    def render(self) -> str:
        lines = [
            f"  PR-AUC              {self.pr_auc:.4f}   (baseline {self.baseline_rate:.4f}"
            f" -> lift {self.lift_over_baseline:.1f}x)",
            f"  ROC-AUC             {self.roc_auc:.4f}",
            f"  Brier score         {self.brier:.5f}",
            f"  Mejor F1            {self.best_f1:.4f} @ umbral {self.best_threshold:.3f}"
            f"  (precision {self.precision_at_best:.3f} / recall {self.recall_at_best:.3f})",
            f"  Muestras            {self.n_samples:,} ({self.n_positives:,} churns)",
        ]
        if self.top_k:
            lines.append("  Priorizando el top-k de cuentas por probabilidad:")
            for row in self.top_k:
                lines.append(
                    f"    top {row['fraction']:>5.1%} ({row['n_accounts']:>6,} cuentas):"
                    f" precision {row['precision']:.3f} | recall {row['recall']:.3f}"
                    f" | lift {row['lift']:.1f}x"
                )
        return "\n".join(lines)


def evaluate_predictions(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    top_k_fractions: list[float] | None = None,
) -> EvaluationResult:
    """Calcula el set completo de metricas para un conjunto de predicciones."""
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)

    baseline = float(y_true.mean())
    pr_auc = float(average_precision_score(y_true, y_prob))
    roc_auc = float(roc_auc_score(y_true, y_prob))

    precision, recall, thresholds = precision_recall_curve(y_true, y_prob)
    with np.errstate(divide="ignore", invalid="ignore"):
        f1 = np.nan_to_num(2 * precision * recall / (precision + recall))
    best_idx = int(np.argmax(f1[:-1])) if len(f1) > 1 else 0
    best_threshold = float(thresholds[best_idx]) if len(thresholds) else 0.5

    y_pred = (y_prob >= best_threshold).astype(int)

    return EvaluationResult(
        pr_auc=pr_auc,
        roc_auc=roc_auc,
        baseline_rate=baseline,
        lift_over_baseline=pr_auc / baseline if baseline else 0.0,
        brier=float(brier_score_loss(y_true, y_prob)),
        best_threshold=best_threshold,
        best_f1=float(f1_score(y_true, y_pred, zero_division=0)),
        precision_at_best=float(precision_score(y_true, y_pred, zero_division=0)),
        recall_at_best=float(recall_score(y_true, y_pred, zero_division=0)),
        top_k=_top_k_metrics(y_true, y_prob, top_k_fractions or [0.01, 0.05, 0.10, 0.20]),
        n_samples=int(len(y_true)),
        n_positives=int(y_true.sum()),
    )


def _top_k_metrics(
    y_true: np.ndarray, y_prob: np.ndarray, fractions: list[float]
) -> list[dict[str, Any]]:
    """Precision y recall si CX solo trabaja el top-k% de cuentas mas riesgosas."""
    order = np.argsort(-y_prob)
    sorted_true = y_true[order]
    total_positives = int(y_true.sum())
    baseline = float(y_true.mean())
    out = []
    for frac in fractions:
        k = max(1, int(round(len(y_true) * frac)))
        hits = int(sorted_true[:k].sum())
        precision = hits / k
        out.append(
            {
                "fraction": frac,
                "n_accounts": k,
                "precision": round(precision, 4),
                "recall": round(hits / total_positives, 4) if total_positives else 0.0,
                "lift": round(precision / baseline, 2) if baseline else 0.0,
            }
        )
    return out
