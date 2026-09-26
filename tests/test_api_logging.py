"""Logs estructurados de la API.

Lo que se prueba es lo que despues se consulta en Cloud Logging: que cada request deje una
linea con campos, que esa linea sea JSON valido y que no se filtren ids de cuentas.
"""

import json
import logging

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.main import app  # noqa: E402
from app.observability import JsonFormatter, log_event, usa_json  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

LOGGER = "app.observability"


def eventos(caplog, tipo="request"):
    return [
        r.structured
        for r in caplog.records
        if (getattr(r, "structured", None) or {}).get("event") == tipo
    ]


def test_cada_request_deja_una_linea_con_latencia(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/api/v1/periodos")

    assert len(eventos(caplog)) == 1
    evento = eventos(caplog)[0]
    assert evento["method"] == "GET"
    assert evento["path"] == "/api/v1/periodos"
    assert evento["status"] == 200
    assert evento["latency_ms"] >= 0


def test_no_se_loguea_el_id_de_la_cuenta(caplog):
    """Se registra la ruta del endpoint, no la concreta: el id no llega a los logs."""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/api/v1/accounts/987654", params={"periodo": 190001})

    evento = eventos(caplog)[0]
    assert evento["path"] == "/api/v1/accounts/{account_id}"
    assert "987654" not in json.dumps(evento)
    # El periodo si, que describe el pedido y no a nadie.
    assert evento["query_periodo"] == "190001"


def test_un_error_del_cliente_se_loguea_como_warning(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/api/v1/accounts/1", params={"periodo": 190001})

    registro = next(r for r in caplog.records if (getattr(r, "structured", None) or {}))
    assert registro.levelno == logging.WARNING
    assert eventos(caplog)[0]["status"] == 404


def test_health_no_ensucia_los_logs(caplog):
    """Cloud Run lo llama todo el tiempo: queda en DEBUG."""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/health")

    assert eventos(caplog) == []


def test_la_linea_es_json_con_los_campos_de_cloud_logging():
    registro = logging.LogRecord(
        name="app.observability",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="GET /api/v1/summary 200",
        args=(),
        exc_info=None,
    )
    registro.structured = {"event": "request", "status": 200, "latency_ms": 12.3}

    payload = json.loads(JsonFormatter().format(registro))

    assert payload["severity"] == "INFO"
    assert payload["message"] == "GET /api/v1/summary 200"
    assert payload["status"] == 200
    assert payload["latency_ms"] == 12.3


def test_log_event_agrega_los_campos_al_registro(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        log_event(logging.INFO, "scoring servido", event="batch", n_accounts=32071)

    assert eventos(caplog, "batch")[0]["n_accounts"] == 32071


@pytest.mark.parametrize(
    ("modo", "en_cloud_run", "esperado"),
    [("json", False, True), ("text", True, False), ("auto", True, True), ("auto", False, False)],
)
def test_json_solo_en_la_nube_salvo_que_se_fuerce(monkeypatch, modo, en_cloud_run, esperado):
    monkeypatch.setattr("app.observability.SERVICE", "churn-demo" if en_cloud_run else None)
    assert usa_json(modo) is esperado
