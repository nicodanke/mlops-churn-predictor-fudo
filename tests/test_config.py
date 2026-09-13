"""Herencia entre archivos de configuracion (`extends`)."""

import pytest

from churn.config import PROJECT_ROOT, Config


def escribir(ruta, texto):
    ruta.write_text(texto)
    return ruta


def test_hereda_la_base_y_pisa_solo_lo_que_redefine(tmp_path):
    escribir(
        tmp_path / "base.yaml",
        "data:\n  raw_path: data/\n  features_path: out/f.parquet\n"
        "model:\n  params:\n    max_depth: 6\n",
    )
    hija = escribir(tmp_path / "gcp.yaml", "extends: base.yaml\ndata:\n  raw_path: /mnt/gcs/raw/\n")

    cfg = Config.load(hija)

    assert cfg.get("data.raw_path") == "/mnt/gcs/raw/"
    assert cfg.get("data.features_path") == "out/f.parquet"
    assert cfg.get("model.params.max_depth") == 6
    assert "extends" not in cfg.raw


def test_las_listas_se_reemplazan_enteras(tmp_path):
    escribir(tmp_path / "base.yaml", "features:\n  lags: [1, 2, 3]\n")
    hija = escribir(tmp_path / "hija.yaml", "extends: base.yaml\nfeatures:\n  lags: [1]\n")

    assert Config.load(hija).get("features.lags") == [1]


def test_herencia_circular_falla_con_un_mensaje_claro(tmp_path):
    escribir(tmp_path / "a.yaml", "extends: b.yaml\n")
    escribir(tmp_path / "b.yaml", "extends: a.yaml\n")

    with pytest.raises(ValueError, match="circular"):
        Config.load(tmp_path / "a.yaml")


def test_la_config_de_gcp_entrena_con_los_mismos_parametros_que_la_local():
    """Si la config de la nube copiara los hiperparametros, un cambio en model.yaml no
    llegaria al reentrenamiento que dispara CI."""
    local = Config.load(PROJECT_ROOT / "config" / "model.yaml")
    gcp = Config.load(PROJECT_ROOT / "config" / "gcp.yaml")

    for seccion in ("labeling", "features", "split", "model", "evaluation", "promotion", "risk"):
        assert gcp.get(seccion) == local.get(seccion), seccion
    assert gcp.get("data.raw_path").startswith("/mnt/gcs/")
    assert gcp.get("scoring.output_dir").startswith("/mnt/gcs/")
