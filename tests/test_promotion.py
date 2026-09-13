"""Champion / challenger: cuando un modelo reentrenado reemplaza al de produccion."""

import json

import numpy as np
import pandas as pd
import pytest

from churn.config import Config
from churn.models.artifact import ModelArtifact, save_model
from churn.models.promotion import (
    METRICAS_GUARDADAS,
    MISMO_TEST,
    SIN_CAMPEON,
    PromotionPolicy,
    challenge,
    decide,
    promote,
    write_decision,
)
from churn.models.split import holdout_frame

POLICY = PromotionPolicy(
    metric="pr_auc", min_pr_auc=0.20, min_improvement=0.005, max_regression={"roc_auc": 0.01}
)

CFG = Config(
    raw={
        "split": {"n_test_periods": 2, "n_val_periods": 2},
        "labeling": {"min_months_in_panel": 2},
        "evaluation": {"min_pr_auc": 0.20, "top_k_fractions": [0.05]},
        "promotion": {"metric": "pr_auc", "min_improvement": 0.005},
    }
)


def metricas(pr_auc, roc_auc=0.95):
    return {"pr_auc": pr_auc, "roc_auc": roc_auc, "best_f1": 0.5, "brier": 0.02}


class ColumnaComoScore:
    """Modelo de juguete: la probabilidad de churn es una columna del input."""

    def __init__(self, columna):
        self.columna = columna

    def predict_proba(self, X):
        p = X[self.columna].to_numpy(dtype=float)
        return np.column_stack([1 - p, p])


def artefacto(columna, metricas_test):
    return ModelArtifact(
        model=ColumnaComoScore(columna),
        calibrator=None,
        feature_names=[columna],
        categorical_features=[],
        category_levels={},
        metrics={"test": metricas_test},
    )


def panel(cuentas_por_periodo=200):
    rng = np.random.default_rng(0)
    periodos = [202401, 202402, 202403, 202404, 202405, 202406]
    n = cuentas_por_periodo * len(periodos)
    churn = (rng.random(n) < 0.2).astype(float)
    return pd.DataFrame(
        {
            "id": np.tile(np.arange(cuentas_por_periodo), len(periodos)),
            "periodo": np.repeat(periodos, cuentas_por_periodo),
            "months_in_panel": 5,
            "churn": churn,
            "perfecto": churn,
            "ruido": rng.random(n),
        }
    )


# ------------------------------------------------------------------ decide --


def test_sin_campeon_alcanza_con_el_umbral_de_negocio():
    decision = decide(metricas(0.30), None, POLICY)
    assert decision.promote
    assert decision.comparison == SIN_CAMPEON


def test_sin_campeon_bajo_el_umbral_no_se_promueve():
    assert not decide(metricas(0.15), None, POLICY).promote


def test_una_mejora_menor_al_margen_no_reemplaza_al_campeon():
    assert not decide(metricas(0.650), metricas(0.648), POLICY).promote


def test_una_mejora_suficiente_reemplaza_al_campeon():
    assert decide(metricas(0.660), metricas(0.650), POLICY).promote


def test_mejorar_pr_auc_rompiendo_roc_auc_se_bloquea():
    decision = decide(metricas(0.70, roc_auc=0.93), metricas(0.65, roc_auc=0.96), POLICY)
    assert not decision.promote
    assert [c.name for c in decision.checks if not c.passed] == ["regresion_roc_auc"]


def test_forzar_saltea_la_comparacion_pero_nunca_el_umbral_de_negocio():
    assert decide(metricas(0.60), metricas(0.65), POLICY, force=True).promote
    assert not decide(metricas(0.10), metricas(0.65), POLICY, force=True).promote


def test_la_politica_rechaza_metricas_donde_menos_es_mejor():
    with pytest.raises(ValueError, match="brier"):
        PromotionPolicy.from_config(Config(raw={"promotion": {"metric": "brier"}}))


# --------------------------------------------------------------- challenge --


def test_holdout_son_los_ultimos_periodos_etiquetables():
    test = holdout_frame(panel(), n_test_periods=2, n_val_periods=2)
    assert sorted(test["periodo"].unique()) == [202405, 202406]


def test_el_campeon_se_reevalua_sobre_el_mismo_test_y_no_con_sus_metricas_viejas(tmp_path):
    # Las metricas guardadas del campeon dicen 0.10, pero en el test actual es perfecto.
    save_model(artefacto("perfecto", metricas(0.10)), tmp_path / "champion")
    candidato = artefacto("ruido", metricas(0.90))

    decision = challenge(panel(), CFG, candidato, tmp_path / "champion", version="abc")

    assert decision.comparison == MISMO_TEST
    assert decision.champion["pr_auc"] == 1.0
    assert not decision.promote


def test_si_al_campeon_le_faltan_features_se_comparan_sus_metricas_guardadas(tmp_path):
    save_model(artefacto("feature_que_ya_no_existe", metricas(0.50)), tmp_path / "champion")

    decision = challenge(panel(), CFG, artefacto("ruido", metricas(0.60)), tmp_path / "champion")

    assert decision.comparison == METRICAS_GUARDADAS
    assert decision.champion["pr_auc"] == 0.50
    assert decision.promote


def test_sin_directorio_de_campeon_es_la_primera_version(tmp_path):
    decision = challenge(panel(), CFG, artefacto("ruido", metricas(0.30)), tmp_path / "vacio")
    assert decision.comparison == SIN_CAMPEON
    assert decision.promote


# ----------------------------------------------------------------- promote --


def candidato_en_disco(models, version, pr_auc):
    directorio = models / "candidates" / version
    save_model(artefacto("perfecto", metricas(pr_auc)), directorio)
    write_decision(decide(metricas(pr_auc), None, POLICY, version=version), directorio)
    return directorio


def test_promover_publica_el_campeon_y_anota_el_historial(tmp_path):
    models = tmp_path / "models"
    for version, pr_auc in (("v1", 0.50), ("v2", 0.60)):
        promote(candidato_en_disco(models, version, pr_auc), models / "champion")

    registry = json.loads((models / "registry.json").read_text())
    assert registry["champion"] == "v2"
    assert [h["version"] for h in registry["history"]] == ["v1", "v2"]
    assert registry["history"][1]["previous_version"] == "v1"

    release = json.loads((models / "champion" / "release.json").read_text())
    assert release["version"] == "v2"
    assert (models / "champion" / "churn_model.joblib").exists()
    # Cada campeon queda archivado aunque despues se borre su candidato.
    assert (models / "releases" / "v1" / "churn_model.joblib").exists()


def test_rollback_desde_una_release_archivada(tmp_path):
    models = tmp_path / "models"
    for version, pr_auc in (("v1", 0.50), ("v2", 0.60)):
        promote(candidato_en_disco(models, version, pr_auc), models / "champion")

    release = promote(models / "releases" / "v1", models / "champion", force=True)

    assert release["version"] == "v1"
    assert release["previous_version"] == "v2"
    assert json.loads((models / "registry.json").read_text())["champion"] == "v1"


def test_no_se_promueve_un_candidato_que_perdio_salvo_que_se_fuerce(tmp_path):
    models = tmp_path / "models"
    candidato = candidato_en_disco(models, "v3", 0.10)  # bajo el umbral de negocio

    with pytest.raises(ValueError, match="no promover"):
        promote(candidato, models / "champion")
    assert not (models / "champion").exists()

    release = promote(candidato, models / "champion", force=True)
    assert release["forced"]
