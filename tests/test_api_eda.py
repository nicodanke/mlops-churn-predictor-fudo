"""El endpoint /api/v1/eda sirve el JSON que dejo `churn eda`, y lo relee si cambia.

Que lo relea importa: el reporte se regenera con cada batch mensual, y en Cloud Run el
servicio escala a cero y vuelve, pero tambien puede quedar vivo entre dos corridas. Sin
relectura seguiria mostrando la base del mes anterior sin avisar.
"""

import json
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.store import EdaStore  # noqa: E402


def reporte(cuentas: int) -> dict:
    return {
        "generated_at": "2026-09-22T12:00:00+00:00",
        "panel": {
            "filas": 100,
            "cuentas": cuentas,
            "periodos": 3,
            "periodo_desde": 202501,
            "periodo_hasta": 202503,
            "periodo_referencia": 202503,
        },
        "base": {
            "serie": [],
            "cuentas_activas_ultimo": cuentas,
            "cuentas_activas_primero": cuentas,
            "crecimiento_mensual": 0.0,
            "altas_totales": 0,
            "bajas_confirmadas_totales": 0,
            "churn_rate_promedio": 3.7,
            "filas_etiquetadas": 100,
            "filas_ambiguas": 0,
        },
        "adopcion": {"periodo": 202503, "cuentas": cuentas, "grupos": [], "volumen": []},
        "churn": {"n_se_queda": 90, "n_churn": 10, "senales": []},
    }


def test_sin_reporte_avisa_como_generarlo(tmp_path):
    store = EdaStore(tmp_path)
    assert store.available() is False
    with pytest.raises(FileNotFoundError, match="churn eda"):
        store.load()


def test_relee_el_reporte_cuando_se_regenera(tmp_path):
    destino = tmp_path / "stats.json"
    destino.write_text(json.dumps(reporte(100)))

    store = EdaStore(tmp_path)
    assert store.load()["panel"]["cuentas"] == 100

    # mtime tiene resolucion de segundo en algunos sistemas de archivos: sin el
    # adelanto explicito el test pasaria por casualidad y no por la comprobacion.
    destino.write_text(json.dumps(reporte(200)))
    import os

    futuro = time.time() + 2
    os.utime(destino, (futuro, futuro))

    assert store.load()["panel"]["cuentas"] == 200


def test_el_endpoint_devuelve_el_reporte(tmp_path, monkeypatch):
    (tmp_path / "stats.json").write_text(json.dumps(reporte(33517)))

    from app import main

    monkeypatch.setattr(main, "eda_store", EdaStore(tmp_path))
    from fastapi.testclient import TestClient

    respuesta = TestClient(main.app).get("/api/v1/eda")
    assert respuesta.status_code == 200
    assert respuesta.json()["panel"]["cuentas"] == 33517
    assert respuesta.json()["base"]["churn_rate_promedio"] == 3.7


def test_sin_reporte_el_endpoint_responde_404(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "eda_store", EdaStore(tmp_path))
    from fastapi.testclient import TestClient

    respuesta = TestClient(main.app).get("/api/v1/eda")
    assert respuesta.status_code == 404
    assert "churn eda" in respuesta.json()["detail"]
