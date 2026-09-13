"""Persistencia del modelo: en disco local y en un bucket."""

import json

import numpy as np
import pandas as pd
import pytest

from churn.models.artifact import (
    METADATA_FILENAME,
    MODEL_FILENAME,
    ModelArtifact,
    load_model,
    save_model,
)


class ProbabilidadFija:
    """Modelo de juguete: la misma probabilidad de churn para todas las cuentas."""

    def predict_proba(self, X):
        p = np.full(len(X), 0.25)
        return np.column_stack([1 - p, p])


def artefacto():
    return ModelArtifact(
        model=ProbabilidadFija(),
        calibrator=None,
        feature_names=["ventas_totales"],
        categorical_features=[],
        category_levels={},
        metrics={"test": {"pr_auc": 0.65}},
        config={"nota": "señal de baja"},
        decision_threshold=0.3,
    )


def comprobar_modelo(cargado):
    assert cargado.decision_threshold == 0.3
    assert cargado.feature_names == ["ventas_totales"]
    X = pd.DataFrame({"ventas_totales": [100.0, 0.0]})
    assert cargado.predict_proba(X).tolist() == [0.25, 0.25]


def test_guarda_y_carga_en_disco(tmp_path):
    directorio = tmp_path / "models" / "nuevo"

    save_model(artefacto(), directorio)

    comprobar_modelo(load_model(directorio))
    metadata = json.loads((directorio / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert metadata["config"]["nota"] == "señal de baja"


def test_guarda_y_carga_en_un_bucket(tmp_path):
    fsspec = pytest.importorskip("fsspec")
    # memory:// se comporta como gs:// sin necesitar GCP: mismo camino de codigo.
    url = f"memory://bucket/models/vm/{tmp_path.name}"

    ruta = save_model(artefacto(), url)

    assert ruta == f"{url}/{MODEL_FILENAME}"
    fs = fsspec.filesystem("memory")
    assert fs.exists(f"{url}/{METADATA_FILENAME}")
    comprobar_modelo(load_model(url))


def test_sin_modelo_avisa_que_hay_que_entrenar(tmp_path):
    with pytest.raises(FileNotFoundError, match="make train"):
        load_model(tmp_path)
