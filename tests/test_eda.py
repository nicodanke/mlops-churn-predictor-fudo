"""El reporte de EDA alimenta el dashboard: si un numero sale mal, sale mal a la vista.

Lo que se verifica aca son las decisiones de conteo que no son obvias leyendo el codigo:
que un null cuente como "no usa", que la primera cohorte no se cuente como altas, y que
una tasa calculada sobre pocas cuentas no se publique.
"""

import numpy as np
import pandas as pd

from churn.eda.stats import MIN_FILAS_PARA_TASA, build_report


def panel(n_cuentas=200, n_periodos=6, churn_de=()):
    """Panel sintetico con las columnas que mira el reporte.

    `churn_de` son ids que se dan de baja en el penultimo periodo etiquetable.
    """
    filas = []
    for cuenta in range(1, n_cuentas + 1):
        for t in range(n_periodos):
            filas.append({"id": cuenta, "t": t, "periodo": 202501 + t})
    df = pd.DataFrame(filas)
    df["months_in_panel"] = df.groupby("id").cumcount() + 1
    df["churn"] = 0.0
    df.loc[df["t"] >= n_periodos - 1, "churn"] = np.nan  # sin futuro observado
    df.loc[df["id"].isin(churn_de) & (df["t"] == n_periodos - 2), "churn"] = 1.0
    df["is_ambiguous"] = False
    df["pais"] = "Argentina"
    df["estado"] = "ACTIVE"
    df["tenure_months"] = 12.0
    # Una funcionalidad que la mitad de las cuentas usa, escrita como null cuando no.
    df["arqueos"] = np.where(df["id"] % 2 == 0, 5.0, np.nan)
    df["ventas_totales"] = 100.0
    return df


def test_null_cuenta_como_no_usa():
    """En el snapshot "no lo uso este mes" viene como null, no como cero."""
    adopcion = build_report(panel())["adopcion"]
    arqueos = next(
        item
        for grupo in adopcion["grupos"]
        for item in grupo["items"]
        if item["columna"] == "arqueos"
    )
    assert arqueos["pct"] == 50.0
    # La mediana se calcula solo entre quienes la usan, no sobre la base entera.
    assert arqueos["mediana_entre_usuarios"] == 5.0


def test_la_cohorte_inicial_no_son_altas():
    """En el primer periodo todas las cuentas aparecen por primera vez.

    Contarlas como altas inventaria un pico de 26 mil altas en el mes en que arranca la
    ventana de observacion, que es justo el numero que alguien miraria primero.
    """
    serie = build_report(panel(n_cuentas=50))["base"]["serie"]
    assert serie[0]["altas"] == 0
    assert all(fila["altas"] == 0 for fila in serie[1:])  # nadie se suma despues


def test_periodos_sin_futuro_no_reportan_churn():
    """Los ultimos meses no tienen etiqueta: la baja todavia no se puede confirmar."""
    base = build_report(panel(n_periodos=6))["base"]
    assert base["serie"][-1]["churn_rate"] is None
    assert base["serie"][-1]["bajas_confirmadas"] is None
    assert base["periodos_sin_etiqueta"] == [202506]
    # El churn base se calcula solo sobre las filas que si tienen etiqueta.
    assert base["serie"][0]["churn_rate"] == 0.0


def test_churn_base_y_sus_derivados():
    # 20 de 200 cuentas se van en el periodo 202504, sobre 5 periodos etiquetados.
    report = build_report(panel(n_cuentas=200, churn_de=range(1, 21)))
    base = report["base"]
    assert base["bajas_confirmadas_totales"] == 20
    assert base["churn_rate_promedio"] == 2.0  # 20 bajas sobre 1000 filas etiquetadas
    # Con 2% mensual, la base retiene 0.98^12 al año y una cuenta dura 1/0.02 meses.
    assert base["retencion_anual"] == 78.5
    assert base["vida_media_meses"] == 50.0


def test_las_tasas_de_pocas_cuentas_no_se_publican():
    """Un pais con 3 cuentas no tiene un churn rate: tiene ruido."""
    df = panel(n_cuentas=60)
    df.loc[df["id"] <= 3, "pais"] = "Uruguay"
    paises = {fila["pais"]: fila for fila in build_report(df)["base"]["paises"]}
    assert paises["Uruguay"]["cuentas"] == 3
    assert paises["Uruguay"]["churn_rate"] is None
    assert paises["Argentina"]["churn_rate"] is not None


def test_senales_ordenadas_de_la_mas_apagada_a_la_mas_neutra():
    """Lo primero que mira CX es que deja de usarse antes de una baja."""
    df = panel(n_cuentas=200, churn_de=range(1, 101))
    # Las que se van dejan de hacer arqueos; las ventas siguen igual en los dos grupos.
    df.loc[df["id"] <= 100, "arqueos"] = np.nan
    senales = build_report(df)["churn"]["senales"]
    lifts = [s["lift"] for s in senales if s["lift"] is not None]
    assert lifts == sorted(lifts)
    assert senales[0]["columna"] == "arqueos"
    assert senales[0]["adopcion_churn"] == 0.0


def test_el_reporte_es_serializable_a_json():
    """La API lo devuelve tal cual: un np.int64 suelto rompe el endpoint, no el pipeline."""
    import json

    json.dumps(build_report(panel(churn_de=[1, 2, 3])))


def test_el_corte_de_la_tasa_se_cuenta_en_filas_y_no_en_cuentas():
    """20 cuentas observadas 5 meses son 100 observaciones, no 20: alcanzan para la tasa.

    Es la distincion que hace que un pais chico pero con historia larga si tenga numero,
    y un pais recien abierto no.
    """
    periodos_etiquetados = 5
    justas = MIN_FILAS_PARA_TASA // periodos_etiquetados  # 20 cuentas -> 100 filas

    df = panel(n_cuentas=400, n_periodos=6)
    df.loc[df["id"] <= justas, "pais"] = "Chile"
    df.loc[(df["id"] > justas) & (df["id"] <= justas + 3), "pais"] = "Uruguay"

    paises = {fila["pais"]: fila for fila in build_report(df)["base"]["paises"]}
    assert paises["Chile"]["cuentas"] == justas
    assert paises["Chile"]["churn_rate"] is not None
    assert paises["Uruguay"]["churn_rate"] is None


def test_una_funcionalidad_que_nadie_del_grupo_usa_no_tiene_mediana():
    df = panel(n_cuentas=200, churn_de=range(1, 101))
    df.loc[df["id"] <= 100, "arqueos"] = np.nan  # ninguna de las que se van hace arqueos
    arqueos = next(s for s in build_report(df)["churn"]["senales"] if s["columna"] == "arqueos")
    assert arqueos["mediana_churn"] is None
    assert arqueos["mediana_se_queda"] == 5.0
