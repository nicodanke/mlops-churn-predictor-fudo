"""Deteccion de drift con PSI.

Lo que se prueba es lo que despues se lee en el reporte: que una distribucion que no se
movio de cero, que una que se corrio quede marcada como alta, y que los faltantes cuenten
como un tramo mas (una columna que el data warehouse deja de mandar tiene que aparecer).
"""

import numpy as np
import pandas as pd
import pytest

from churn.config import Config
from churn.models.artifact import ModelArtifact
from churn.monitoring.drift import (
    ALTO,
    MODERADO,
    SIN_CAMBIOS,
    compute_drift,
    nivel,
    psi_categorico,
    psi_numerico,
)

CFG = Config(raw={"monitoring": {"psi_bins": 10, "umbral_moderado": 0.10, "umbral_alto": 0.25}})


def serie(valores):
    return pd.Series(valores, dtype="float64")


def artefacto(feature_names, training_periods, categoricas=()):
    return ModelArtifact(
        model=None,
        calibrator=None,
        feature_names=list(feature_names),
        categorical_features=list(categoricas),
        category_levels={},
        training_periods=list(training_periods),
    )


# --------------------------------------------------------------------- PSI --


def test_la_misma_distribucion_no_tiene_drift():
    rng = np.random.default_rng(42)
    valores = serie(rng.normal(100, 15, 5_000))

    assert psi_numerico(valores, valores) == pytest.approx(0.0, abs=1e-9)


def test_una_distribucion_corrida_da_psi_alto():
    rng = np.random.default_rng(42)
    referencia = serie(rng.normal(100, 15, 5_000))
    actual = serie(rng.normal(150, 15, 5_000))  # +50 de media

    assert psi_numerico(referencia, actual) > 0.25


def test_un_ruido_chico_no_dispara_la_alarma():
    rng = np.random.default_rng(7)
    referencia = serie(rng.normal(100, 15, 20_000))
    actual = serie(rng.normal(101, 15, 20_000))

    assert psi_numerico(referencia, actual) < 0.10


def test_una_columna_que_deja_de_llegar_se_detecta():
    """El DW deja de mandar la columna: todo pasa a faltante y el valor no cambia."""
    referencia = serie([10.0] * 500 + [20.0] * 500)
    actual = pd.Series([np.nan] * 1_000, dtype="float64")

    assert psi_numerico(referencia, actual) > 0.25


def test_una_columna_constante_no_rompe_ni_inventa_drift():
    constante = serie([5.0] * 1_000)

    assert psi_numerico(constante, constante) == pytest.approx(0.0, abs=1e-9)


def test_valores_fuera_del_rango_de_entrenamiento_caen_en_los_extremos():
    """Un mes con valores mas altos que todo lo visto tiene que contar, no descartarse."""
    rng = np.random.default_rng(3)
    referencia = serie(rng.uniform(0, 100, 5_000))
    actual = serie(rng.uniform(500, 600, 5_000))

    assert psi_numerico(referencia, actual) > 0.25


def test_categoricas_comparan_frecuencias():
    referencia = pd.Series(["basico"] * 700 + ["pro"] * 300)
    igual = pd.Series(["basico"] * 70 + ["pro"] * 30)
    cambiada = pd.Series(["basico"] * 200 + ["pro"] * 800)

    assert psi_categorico(referencia, igual) == pytest.approx(0.0, abs=1e-3)
    assert psi_categorico(referencia, cambiada) > 0.25


def test_los_niveles_siguen_los_umbrales():
    assert nivel(0.05, 0.10, 0.25) == SIN_CAMBIOS
    assert nivel(0.10, 0.10, 0.25) == MODERADO
    assert nivel(0.30, 0.10, 0.25) == ALTO


# ------------------------------------------------------------------ reporte --


def panel_con_una_feature_corrida():
    """Tres meses de entrenamiento estables y un mes actual con las ventas por el piso."""
    rng = np.random.default_rng(11)
    partes = []
    for periodo in (202401, 202402, 202403):
        partes.append(
            pd.DataFrame(
                {
                    "id": range(500),
                    "periodo": periodo,
                    "ventas_totales": rng.normal(1_000, 100, 500),
                    "usuarios": rng.normal(5, 1, 500),
                    "plan_base": pd.Categorical(["basico"] * 350 + ["pro"] * 150),
                }
            )
        )
    partes.append(
        pd.DataFrame(
            {
                "id": range(500),
                "periodo": 202404,
                "ventas_totales": rng.normal(200, 100, 500),  # se desplomaron
                "usuarios": rng.normal(5, 1, 500),
                "plan_base": pd.Categorical(["basico"] * 350 + ["pro"] * 150),
            }
        )
    )
    return pd.concat(partes, ignore_index=True)


def test_el_reporte_ordena_por_psi_y_marca_la_feature_que_se_movio():
    features = panel_con_una_feature_corrida()
    modelo = artefacto(["ventas_totales", "usuarios", "plan_base"], [202401, 202402, 202403])

    reporte = compute_drift(features, modelo, CFG)

    assert reporte.periodo_actual == 202404
    assert reporte.periodos_referencia == [202401, 202402, 202403]
    assert reporte.tabla.iloc[0]["feature"] == "ventas_totales"
    assert reporte.tabla.iloc[0]["nivel"] == ALTO
    assert reporte.hay_drift
    assert set(reporte.tabla["feature"]) == {"ventas_totales", "usuarios", "plan_base"}


def test_sin_cambios_el_reporte_no_avisa_nada():
    features = panel_con_una_feature_corrida()
    modelo = artefacto(["usuarios", "plan_base"], [202401, 202402, 202403])

    reporte = compute_drift(features, modelo, CFG)

    assert not reporte.hay_drift
    assert reporte.altos.empty


def test_se_puede_pedir_un_periodo_puntual():
    features = panel_con_una_feature_corrida()
    modelo = artefacto(["ventas_totales"], [202401, 202402, 202403])

    reporte = compute_drift(features, modelo, CFG, periodo=202403)

    assert reporte.periodo_actual == 202403
    # 202403 es parte del entrenamiento: se saca de la referencia para no compararlo consigo mismo.
    assert reporte.periodos_referencia == [202401, 202402]


def test_un_periodo_que_no_existe_avisa():
    features = panel_con_una_feature_corrida()
    modelo = artefacto(["ventas_totales"], [202401, 202402, 202403])

    with pytest.raises(ValueError, match="199901"):
        compute_drift(features, modelo, CFG, periodo=199901)


def test_el_reporte_se_puede_serializar():
    features = panel_con_una_feature_corrida()
    modelo = artefacto(["ventas_totales", "usuarios"], [202401, 202402, 202403])

    datos = compute_drift(features, modelo, CFG).to_dict()

    assert datos["periodo_actual"] == 202404
    assert datos["hay_drift"] is True
    assert datos["conteo"][ALTO] >= 1
    assert len(datos["features"]) == 2
