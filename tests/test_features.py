"""Feature engineering temporal: lo que hace que el modelo vea tendencias y no niveles."""

import numpy as np
import pandas as pd

from churn.data.schema import normalize_column
from churn.features.builder import build_features
from churn.features.naming import humanize_feature


def base_panel():
    """Panel minimo: una cuenta con ventas que caen y otra estable, con un hueco."""
    rows = []
    for t, ventas in enumerate([100, 90, 60, 20]):
        rows.append(
            {
                "id": 1,
                "t": t,
                "periodo": 202401 + t,
                "v_salon": ventas,
                "usuarios": 5,
                "plan": "adv-tbl-ar",
                "estado": "ACTIVE",
                "estado_severity": 0,
                "months_in_panel": t + 1,
                "productos": 10,
            }
        )
    # La cuenta 2 se saltea t=1: el lag no debe tomar el mes equivocado.
    for t, ventas in zip([0, 2, 3], [50, 55, 60], strict=False):
        rows.append(
            {
                "id": 2,
                "t": t,
                "periodo": 202401 + t,
                "v_salon": ventas,
                "usuarios": 3,
                "plan": "ba-cl",
                "estado": "PENDING_PAYMENT",
                "estado_severity": 2,
                "months_in_panel": 1,
                "productos": 8,
            }
        )
    return pd.DataFrame(rows)


def test_delta_1m_mide_la_variacion_contra_el_mes_anterior():
    out = build_features(base_panel(), lags=[1, 2, 3])
    fila = out[(out["id"] == 1) & (out["t"] == 2)].iloc[0]
    assert fila["v_salon_delta_1m"] == -30.0  # 60 - 90


def test_el_lag_respeta_los_huecos_del_panel():
    """La cuenta 2 no tiene t=1, asi que en t=2 el delta_1m debe quedar vacio."""
    out = build_features(base_panel(), lags=[1, 2, 3])
    fila = out[(out["id"] == 2) & (out["t"] == 2)].iloc[0]
    assert np.isnan(fila["v_salon_delta_1m"])


def test_primera_aparicion_no_tiene_historia():
    out = build_features(base_panel(), lags=[1, 2, 3])
    fila = out[(out["id"] == 1) & (out["t"] == 0)].iloc[0]
    assert np.isnan(fila["v_salon_delta_1m"])


def test_slope_detecta_una_caida_sostenida():
    out = build_features(base_panel(), lags=[1, 2, 3])
    fila = out[(out["id"] == 1) & (out["t"] == 3)].iloc[0]
    assert fila["ventas_totales_slope_3m"] < 0


def test_plan_se_descompone_en_columnas():
    out = build_features(base_panel(), lags=[1])
    fila = out[(out["id"] == 1) & (out["t"] == 0)].iloc[0]
    assert fila["plan_modulo_tbl"] == 1
    assert fila["plan_modulo_kds"] == 0
    assert fila["plan_n_modulos"] == 1
    assert fila["plan_mercado"] == "ar"


def test_meses_consecutivos_con_deuda_acumula():
    out = build_features(base_panel(), lags=[1])
    cuenta2 = out[out["id"] == 2].sort_values("t")
    assert list(cuenta2["meses_consecutivos_con_deuda"]) == [1, 2, 3]


def test_no_hay_columnas_duplicadas():
    out = build_features(base_panel(), lags=[1, 2, 3])
    assert not out.columns.duplicated().any()


def test_normalize_column_limpia_los_headers_del_export():
    assert normalize_column("Pr Con Costo ($)") == "pr_con_costo"
    assert normalize_column("Cat Productos") == "cat_productos"
    assert normalize_column("Sii Cantiad De Boletas") == "sii_cantiad_de_boletas"


def test_humanize_traduce_las_features_derivadas():
    assert humanize_feature("v_salon") == "Ventas en salon"
    assert "variacion vs mes anterior" in humanize_feature("v_salon_delta_1m")
    assert "Mesas" in humanize_feature("plan_modulo_tbl")
