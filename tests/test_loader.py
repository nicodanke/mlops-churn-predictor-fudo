"""Carga del snapshot: un archivo, un directorio o un patron.

El DW puede entregar el historico completo o un archivo por mes, y el pipeline tiene que
dar el mismo resultado en los dos casos.
"""

import pandas as pd
import pytest

from churn.data.loader import load_panel, load_raw, next_period, resolve_sources


def escribir(directorio, nombre, periodos, ids=(1, 2)):
    filas = [
        {"Internal Pk": i * 100 + p, "Periodo": p, "ID": i, "Nombre": f"cuenta {i}",
         "Fecha": "2020-01-01 00:00:00 +0000", "Plan": "adv-tbl-ar", "Pais": "Argentina",
         "Estado": "ACTIVE", "Estado Comercial": "ACTIVE", "Mesas": 10, "Usuarios": 3}
        for p in periodos
        for i in ids
    ]
    ruta = directorio / nombre
    pd.DataFrame(filas).to_csv(ruta, index=False)
    return ruta


def test_lee_un_archivo_suelto(tmp_path):
    ruta = escribir(tmp_path, "stats.csv", [202401, 202402])
    df = load_raw(ruta)
    assert len(df) == 4
    assert "internal_pk" in df.columns  # headers normalizados a snake_case


def test_lee_todos_los_archivos_de_un_directorio(tmp_path):
    """El caso del uso mensual: se deja caer el archivo del mes nuevo y listo."""
    escribir(tmp_path, "stats_2024.csv", [202401, 202402])
    escribir(tmp_path, "stats_202403.csv", [202403])

    df = load_raw(tmp_path)
    assert sorted(df["periodo"].unique()) == [202401, 202402, 202403]
    assert len(df) == 6


def test_acepta_un_patron(tmp_path):
    escribir(tmp_path, "stats_a.csv", [202401])
    escribir(tmp_path, "stats_b.csv", [202402])
    escribir(tmp_path, "otro.csv", [209901])

    df = load_raw(str(tmp_path / "stats_*.csv"))
    assert sorted(df["periodo"].unique()) == [202401, 202402]


def test_un_periodo_repetido_gana_el_ultimo_archivo(tmp_path):
    """Permite reemplazar un mes ya cargado sin borrar nada."""
    escribir(tmp_path, "a_viejo.csv", [202401])
    ruta = escribir(tmp_path, "b_corregido.csv", [202401])
    corregido = pd.read_csv(ruta)
    corregido["Mesas"] = 99
    corregido.to_csv(ruta, index=False)

    df = load_raw(tmp_path)
    assert len(df) == 2  # no se duplican las cuentas
    assert (df["mesas"] == 99).all()


def test_esquemas_distintos_no_rompen(tmp_path, caplog):
    """Si el DW agrega una metrica, los meses viejos quedan en NaN pero se avisa."""
    escribir(tmp_path, "a.csv", [202401])
    ruta = escribir(tmp_path, "b.csv", [202402])
    extra = pd.read_csv(ruta)
    extra["Metrica Nueva"] = 7
    extra.to_csv(ruta, index=False)

    df = load_raw(tmp_path)
    assert "metrica_nueva" in df.columns
    assert df.loc[df["periodo"] == 202401, "metrica_nueva"].isna().all()
    assert "esquema distinto" in caplog.text


def test_directorio_vacio_da_un_error_claro(tmp_path):
    with pytest.raises(FileNotFoundError, match="data/README.md"):
        resolve_sources(tmp_path)


def test_rutas_remotas_se_pasan_tal_cual():
    assert resolve_sources("gs://bucket/stats.csv") == ["gs://bucket/stats.csv"]


def test_el_panel_indexa_los_periodos_de_forma_densa(tmp_path):
    escribir(tmp_path, "stats.csv", [202401, 202402, 202403])
    panel = load_panel(tmp_path)
    assert sorted(panel["t"].unique()) == [0, 1, 2]
    assert panel["tenure_months"].notna().all()


def test_next_period_cruza_el_fin_de_año():
    assert next_period(202412) == 202501
    assert next_period(202601) == 202602


# ---------------------------------------------------------------------------
# Meses faltantes en el medio de la serie
# ---------------------------------------------------------------------------
#
# El indice `t` es denso sobre los periodos presentes, no sobre el calendario. Un mes que
# falta no rompe nada visiblemente: deja los meses vecinos como consecutivos y todo lo que
# depende de "el mes anterior" se corre uno. Es el resultado tipico de subir los snapshots
# de a uno al bucket y saltearse alguno.


def test_avisa_cuando_falta_un_mes_en_el_medio(tmp_path, caplog):
    escribir(tmp_path, "a.csv", [202501, 202502])
    escribir(tmp_path, "b.csv", [202504])  # falta 202503

    with caplog.at_level("WARNING"):
        load_panel(tmp_path)

    assert "202503" in caplog.text
    assert "Faltan 1 periodo" in caplog.text


def test_avisa_de_varios_meses_incluso_cruzando_el_año(tmp_path, caplog):
    escribir(tmp_path, "a.csv", [202511, 202512])
    escribir(tmp_path, "b.csv", [202603])  # faltan 202601 y 202602

    with caplog.at_level("WARNING"):
        load_panel(tmp_path)

    assert "202601, 202602" in caplog.text


def test_una_serie_completa_no_avisa(tmp_path, caplog):
    # Diciembre a enero es consecutivo: el chequeo no puede contar 202513.
    escribir(tmp_path, "a.csv", [202511, 202512, 202601])

    with caplog.at_level("WARNING"):
        load_panel(tmp_path)

    assert "Faltan" not in caplog.text


def test_el_hueco_de_una_cuenta_no_es_un_mes_faltante(tmp_path, caplog):
    """Que una cuenta pause no es lo mismo que falte el snapshot de un mes."""
    escribir(tmp_path, "a.csv", [202501, 202502, 202503], ids=(1,))
    escribir(tmp_path, "b.csv", [202502], ids=(2,))  # la cuenta 2 solo aparece un mes

    with caplog.at_level("WARNING"):
        panel = load_panel(tmp_path)

    assert "Faltan" not in caplog.text
    assert sorted(panel["periodo"].unique()) == [202501, 202502, 202503]
