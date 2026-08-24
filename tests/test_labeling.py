"""La etiqueta de churn se infiere de la presencia en el snapshot: hay que verificarla."""

import numpy as np
import pandas as pd
import pytest

from churn.data.labeling import build_labels


def panel(rows):
    """rows: lista de (id, t). Construye el panel minimo que necesita build_labels."""
    df = pd.DataFrame(rows, columns=["id", "t"])
    df["periodo"] = 202401 + df["t"]
    return df.sort_values(["id", "t"]).reset_index(drop=True)


def label_of(df, account_id, t):
    row = df[(df["id"] == account_id) & (df["t"] == t)]
    return row["churn"].iloc[0]


def test_cuenta_presente_el_mes_siguiente_no_es_churn():
    # La cuenta 1 aparece todos los meses de 0 a 6.
    df = build_labels(panel([(1, t) for t in range(7)]), confirm_window=3)
    assert label_of(df, 1, 0) == 0
    assert label_of(df, 1, 3) == 0


def test_cuenta_que_desaparece_y_no_vuelve_es_churn():
    # La cuenta 2 esta hasta t=2 y no vuelve; hay observacion hasta t=6.
    rows = [(1, t) for t in range(7)] + [(2, t) for t in range(3)]
    df = build_labels(panel(rows), confirm_window=3)
    assert label_of(df, 2, 2) == 1
    assert label_of(df, 2, 1) == 0


def test_pausa_dentro_de_la_ventana_no_cuenta_como_churn():
    """Un restaurante estacional que vuelve al mes siguiente no es una baja."""
    rows = [(1, t) for t in range(7)] + [(3, 0), (3, 1), (3, 3), (3, 4)]
    df = build_labels(panel(rows), confirm_window=3, ambiguous_policy="drop")
    fila = df[(df["id"] == 3) & (df["t"] == 1)]
    assert bool(fila["is_ambiguous"].iloc[0]) is True
    assert np.isnan(fila["churn"].iloc[0])  # descartada del entrenamiento


def test_politica_negative_marca_las_ambiguas_como_no_churn():
    rows = [(1, t) for t in range(7)] + [(3, 0), (3, 1), (3, 3), (3, 4)]
    df = build_labels(panel(rows), confirm_window=3, ambiguous_policy="negative")
    assert label_of(df, 3, 1) == 0


def test_los_ultimos_periodos_no_son_etiquetables():
    """Sin futuro observado no se puede confirmar una baja: la etiqueta queda vacia."""
    df = build_labels(panel([(1, t) for t in range(7)]), confirm_window=3)
    ultimos = df[df["t"] > 6 - 3]
    assert ultimos["churn"].isna().all()
    assert not ultimos["is_labelable"].any()


def test_months_in_panel_cuenta_apariciones_no_meses_de_calendario():
    rows = [(3, 0), (3, 1), (3, 4)]
    df = build_labels(panel(rows), confirm_window=1)
    assert list(df[df["id"] == 3]["months_in_panel"]) == [1, 2, 3]


def test_politica_invalida_falla_temprano():
    with pytest.raises(ValueError, match="ambiguous_policy"):
        build_labels(panel([(1, 0)]), ambiguous_policy="lo-que-sea")


def test_el_cache_de_features_se_invalida_si_el_snapshot_es_mas_nuevo(tmp_path):
    """Agregar el mes nuevo a data/ y correr `score` sin --force servia el panel viejo.

    No fallaba: las predicciones simplemente quedaban un mes atrasadas, en silencio.
    """
    import time

    from churn.pipeline import _cache_is_stale

    snapshot = tmp_path / "stats.csv"
    snapshot.write_text("dummy")
    features = tmp_path / "features.parquet"
    features.write_text("dummy")

    assert not _cache_is_stale(str(snapshot), features)

    time.sleep(0.01)
    snapshot.touch()  # llega el archivo del mes nuevo
    assert _cache_is_stale(str(snapshot), features)


def test_las_rutas_remotas_no_se_comprueban():
    from pathlib import Path

    from churn.pipeline import _cache_is_stale

    assert not _cache_is_stale("gs://bucket/stats.csv", Path("/no/existe"))
