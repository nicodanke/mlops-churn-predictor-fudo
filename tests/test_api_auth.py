"""La API sirve el dashboard desde su mismo origen e informa quien inicio sesion en IAP.

Con Identity-Aware Proxy la sesion es una cookie del dominio del servicio: si el
dashboard viviera en otro dominio, sus fetch a la API no la mandarian.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def test_sin_iap_no_hay_usuario():
    assert client.get("/api/v1/me").json() == {"authenticated": False, "email": None}


def test_detras_de_iap_informa_el_email_sin_el_prefijo_del_proveedor():
    respuesta = client.get(
        "/api/v1/me",
        headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:ana@fu.do"},
    )
    assert respuesta.json() == {"authenticated": True, "email": "ana@fu.do"}


def test_el_dashboard_se_sirve_desde_la_api():
    respuesta = client.get("/")
    assert respuesta.status_code == 200
    assert "Riesgo de Churn" in respuesta.text


def test_el_dashboard_le_habla_a_su_propio_origen():
    respuesta = client.get("/config.js")
    assert "window.location.origin" in respuesta.text
    assert respuesta.headers["cache-control"] == "no-store"


def test_las_rutas_de_la_api_tienen_prioridad_sobre_los_archivos_estaticos():
    respuesta = client.get("/api/v1/periodos")
    assert respuesta.headers["content-type"].startswith("application/json")
