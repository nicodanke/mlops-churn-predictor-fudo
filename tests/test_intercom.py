"""Vinculacion de tickets de Intercom con cuentas del snapshot.

Lo delicado aca es el orden de prioridad: el dato explicito manda, y las vias inferidas
solo completan lo que quedo sin resolver. Si una via debil pisa a una fuerte, se atribuyen
tickets a la cuenta equivocada sin que nada falle.
"""

import pandas as pd
import pytest

from churn.data.intercom import (
    attribute_accounts,
    attribution_report,
    load_companies,
)


def tickets(filas):
    base = {
        "ticket_id": 0, "company_ref": None, "external_id": None,
        "email": None, "ID de dash": None, "periodo": 202401,
    }
    return pd.DataFrame([{**base, **f} for f in filas])


def test_el_atributo_explicito_tiene_prioridad():
    """Si el ticket dice a que cuenta pertenece, ninguna inferencia puede contradecirlo."""
    df = attribute_accounts(
        tickets([{"ID de dash": 111, "external_id": "222", "company_ref": "abc"}]),
        companies=pd.DataFrame({"company_ref": ["abc"], "account_id": [333]}),
        valid_account_ids={111, 222, 333},
    )
    assert df["account_id"].iloc[0] == 111
    assert df["attribution_source"].iloc[0] == "id_de_dash"


def test_la_tabla_de_companies_va_antes_que_el_external_id():
    """La tabla es un dato duro; el external_id es una pista que a veces miente."""
    df = attribute_accounts(
        tickets([{"external_id": "222", "company_ref": "abc"}]),
        companies=pd.DataFrame({"company_ref": ["abc"], "account_id": [333]}),
        valid_account_ids={222, 333},
    )
    assert df["account_id"].iloc[0] == 333
    assert df["attribution_source"].iloc[0] == "tabla_companies"


def test_el_external_id_completa_cuando_no_hay_nada_mejor():
    df = attribute_accounts(
        tickets([{"external_id": "222"}]), valid_account_ids={222}
    )
    assert df["account_id"].iloc[0] == 222
    assert df["attribution_source"].iloc[0] == "external_id"


def test_un_external_id_que_no_es_cuenta_no_se_usa():
    """Los external_id largos son ids de usuario: no hay que confundirlos con cuentas."""
    df = attribute_accounts(
        tickets([{"external_id": "136164424116"}]), valid_account_ids={111, 222}
    )
    assert pd.isna(df["account_id"].iloc[0])


def test_propaga_la_cuenta_de_un_usuario_a_sus_otros_tickets():
    """Si un external_id aparece una vez junto a su cuenta, vale para el resto."""
    df = attribute_accounts(
        tickets([
            {"ID de dash": 111, "external_id": "999888777666"},
            {"external_id": "999888777666"},
        ]),
        valid_account_ids={111},
    )
    assert list(df["account_id"]) == [111, 111]
    assert df["attribution_source"].iloc[1] == "mapa_usuario"


def test_no_propaga_si_la_clave_es_ambigua():
    """Un external_id que aparece con dos cuentas distintas no aporta, confunde."""
    df = attribute_accounts(
        tickets([
            {"ID de dash": 111, "external_id": "999888777666"},
            {"ID de dash": 222, "external_id": "999888777666"},
            {"external_id": "999888777666"},
        ]),
        valid_account_ids={111, 222},
    )
    assert pd.isna(df["account_id"].iloc[2])


def test_ticket_sin_ninguna_pista_queda_sin_atribuir():
    df = attribute_accounts(tickets([{}]))
    assert pd.isna(df["account_id"].iloc[0])
    assert pd.isna(df["attribution_source"].iloc[0])


def test_el_reporte_cubre_todos_los_tickets():
    df = attribute_accounts(
        tickets([{"ID de dash": 111}, {"external_id": "222"}, {}]),
        valid_account_ids={111, 222},
    )
    reporte = attribution_report(df)
    assert reporte["tickets"].sum() == 3
    assert "sin atribuir" in set(reporte["via"])


# --- lectura del export de companies -------------------------------------


def escribir_companies(tmp_path, columnas):
    ruta = tmp_path / "companies.csv"
    pd.DataFrame(columnas).to_csv(ruta, index=False)
    return ruta


OBJ_A = "689bd3ec713afb9ce2284b31"
OBJ_B = "688e5f4852228841f6f312a6"


def test_detecta_el_objectid_y_el_id_de_fudo(tmp_path):
    """Intercom distingue `id` (su ObjectId) de `company_id` (el id externo, el de Fudo)."""
    ruta = escribir_companies(tmp_path, {
        "id": [OBJ_A, OBJ_B], "company_id": [155702, 289196], "name": ["A", "B"],
    })
    comp = load_companies(ruta)
    assert list(comp["company_ref"]) == [OBJ_A, OBJ_B]
    assert list(comp["account_id"]) == [155702, 289196]


def test_reconoce_el_id_de_fudo_con_otro_nombre(tmp_path):
    ruta = escribir_companies(tmp_path, {
        "id": [OBJ_A], "ID de dash": [155702], "name": ["A"],
    })
    assert load_companies(ruta)["account_id"].iloc[0] == 155702


def test_descarta_companies_sin_id_de_fudo(tmp_path):
    ruta = escribir_companies(tmp_path, {
        "id": [OBJ_A, OBJ_B], "company_id": [155702, None],
    })
    comp = load_companies(ruta)
    assert len(comp) == 1


def test_falla_claro_si_no_hay_objectid(tmp_path):
    ruta = escribir_companies(tmp_path, {"nombre": ["A"], "company_id": [155702]})
    with pytest.raises(ValueError, match="id interno de Intercom"):
        load_companies(ruta)


def test_companies_repetidas_no_duplican_el_mapeo(tmp_path):
    ruta = escribir_companies(tmp_path, {
        "id": [OBJ_A, OBJ_A], "company_id": [155702, 155702],
    })
    assert len(load_companies(ruta)) == 1
