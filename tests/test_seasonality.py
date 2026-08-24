"""Deteccion de cuentas con estacionalidad de uso.

Lo que mas facil se rompe aca es la deteccion de episodios: hay que contar tramos de
ausencia *entre* apariciones, no cualquier hueco, y sin confundir la ultima desaparicion
(que puede ser una baja) con una pausa.
"""

import pandas as pd

from churn.data.seasonality import (
    SeasonalityConfig,
    account_seasonality,
    find_pause_episodes,
    monthly_profile,
    seasonal_account_ids,
    seasonality_by_country,
)


def panel(apariciones, pais="Argentina"):
    """apariciones: dict id -> lista de t. Arma el panel minimo."""
    filas = [
        {"id": acct, "t": t, "periodo": _periodo(t), "pais": pais, "nombre": f"cuenta {acct}"}
        for acct, ts in apariciones.items()
        for t in ts
    ]
    return pd.DataFrame(filas).sort_values(["id", "t"]).reset_index(drop=True)


def _periodo(t: int) -> int:
    """t=0 es 202401, y de ahi en adelante mes a mes."""
    year, month = divmod(t, 12)
    return (2024 + year) * 100 + month + 1


def test_detecta_una_pausa_entre_dos_apariciones():
    # Presente en t=0,1 · ausente en 2,3 · vuelve en 4
    ep = find_pause_episodes(panel({1: [0, 1, 4, 5]}))
    assert len(ep) == 1
    assert ep.iloc[0]["t_baja"] == 1
    assert ep.iloc[0]["t_alta"] == 4
    assert ep.iloc[0]["duracion"] == 2


def test_la_ultima_desaparicion_no_es_una_pausa():
    """Que una cuenta deje de aparecer al final puede ser una baja: no cuenta."""
    ep = find_pause_episodes(panel({1: [0, 1, 2]}))
    assert ep.empty


def test_cuenta_continua_no_genera_episodios():
    assert find_pause_episodes(panel({1: list(range(12))})).empty


def test_cuenta_varias_pausas_de_la_misma_cuenta():
    ep = find_pause_episodes(panel({1: [0, 3, 6, 9]}))
    assert len(ep) == 3
    assert list(ep["duracion"]) == [2, 2, 2]


def test_no_mezcla_cuentas_distintas():
    """El salto entre la ultima fila de una cuenta y la primera de otra no es una pausa."""
    ep = find_pause_episodes(panel({1: [0, 1], 2: [8, 9]}))
    assert ep.empty


def test_marca_estacional_por_recurrencia():
    # Dos pausas cortas: recurrente, aunque sin firma de temporada.
    perfil = account_seasonality(panel({1: [0, 2, 4, 6]}), SeasonalityConfig(min_episodios=2))
    fila = perfil[perfil["id"] == 1].iloc[0]
    assert fila["n_pausas"] == 3
    assert bool(fila["es_estacional"])
    assert fila["tipo"] == "intermitente"


def test_marca_estacional_por_firma_de_temporada_con_una_sola_pausa():
    """Se va en abril (t=3), vuelve en noviembre (t=10): un parador de playa."""
    perfil = account_seasonality(panel({1: [0, 1, 2, 10, 11, 12]}))
    fila = perfil[perfil["id"] == 1].iloc[0]
    assert fila["n_pausas"] == 1
    assert fila["mes_baja_modal"] == 4
    assert fila["mes_alta_modal"] == 11
    assert bool(fila["firma_temporada"])
    assert fila["tipo"] == "temporada"


def test_una_pausa_fuera_de_temporada_no_alcanza():
    """Una sola pausa corta en pleno verano no es estacionalidad."""
    perfil = account_seasonality(panel({1: [0, 2, 3, 4]}))
    fila = perfil[perfil["id"] == 1].iloc[0]
    assert not bool(fila["firma_temporada"])
    assert not bool(fila["es_estacional"])


def test_firma_unica_se_puede_desactivar():
    config = SeasonalityConfig(aceptar_firma_unica=False)
    perfil = account_seasonality(panel({1: [0, 1, 2, 10, 11, 12]}), config)
    assert not bool(perfil.iloc[0]["es_estacional"])


def test_seasonal_account_ids_devuelve_solo_las_marcadas():
    datos = panel({1: [0, 2, 4, 6], 2: list(range(12))})
    assert seasonal_account_ids(datos) == {1}


def test_panel_sin_pausas_no_rompe():
    vacio = account_seasonality(panel({1: list(range(6))}))
    assert vacio.empty
    assert seasonal_account_ids(panel({1: list(range(6))})) == set()


def test_perfil_mensual_cubre_los_cuatro_eventos():
    datos = panel({1: [0, 1, 2], 2: [0, 3, 4, 5, 6, 7, 8, 9], 3: list(range(10))})
    perfil = monthly_profile(datos, min_future_months=1)
    assert set(perfil["evento"]) == {
        "baja definitiva", "inicio de pausa", "reactivacion", "alta nueva"
    }
    # Cada evento reporta los 12 meses, aunque algunos tengan cero.
    assert (perfil.groupby("evento").size() == 12).all()


def test_resumen_por_pais():
    datos = pd.concat([
        panel({i: [0, 2, 4] for i in range(1, 6)}, pais="Chile"),
        panel({i: list(range(6)) for i in range(6, 11)}, pais="Brasil"),
    ])
    tabla = seasonality_by_country(datos, min_accounts=1)
    chile = tabla[tabla["pais"] == "Chile"].iloc[0]
    assert chile["cuentas"] == 5
    assert chile["con_pausa"] == 5
    assert chile["pct_pausan"] == 100.0
