"""Categoria de riesgo = probabilidad x revenue, contra los cortes de negocio."""

from churn.scoring.risk import RiskBands, assign_risk, risk_summary

BANDS = RiskBands(no_churn_threshold=0.30, alto=40.0, medio=15.0)


def test_probabilidad_baja_es_no_churn_aunque_facture_mucho():
    out = assign_risk([0.10], [1000.0], BANDS)
    assert out["risk_category"].iloc[0] == "no_churn"
    assert out["will_churn"].iloc[0] == 0


def test_cuenta_grande_con_riesgo_cae_en_alto():
    out = assign_risk([0.80], [100.0], BANDS)  # 80 USD en riesgo
    assert out["risk_category"].iloc[0] == "alto"
    assert out["revenue_at_risk"].iloc[0] == 80.0


def test_misma_probabilidad_distinta_categoria_segun_revenue():
    """Es el punto del modelo de negocio: no todas las bajas cuestan lo mismo."""
    out = assign_risk([0.60, 0.60, 0.60], [100.0, 30.0, 5.0], BANDS)
    assert list(out["risk_category"]) == ["alto", "medio", "bajo"]


def test_revenue_faltante_no_rompe_la_categorizacion():
    out = assign_risk([0.90], [None], BANDS)
    assert out["monthly_revenue"].iloc[0] == 0.0
    assert out["risk_category"].iloc[0] == "bajo"


def test_resumen_agrega_por_categoria():
    scored = assign_risk([0.9, 0.8, 0.1], [100.0, 20.0, 50.0], BANDS)
    resumen = risk_summary(scored)
    assert set(resumen["risk_category"]) == {"alto", "medio", "bajo", "no_churn"}
    alto = resumen[resumen["risk_category"] == "alto"].iloc[0]
    assert alto["cuentas"] == 1
    assert alto["revenue_en_riesgo"] == 90.0


def test_resumen_no_deja_nan_en_categorias_vacias():
    """Sin precios cargados, 'alto' y 'medio' quedan en cero y el promedio daria NaN.

    NaN no es JSON valido: el endpoint de resumen respondia 500 y el dashboard no cargaba.
    """
    scored = assign_risk([0.9, 0.1], [0.0, 0.0], BANDS)  # sin revenue: nada llega a medio
    resumen = risk_summary(scored)

    assert set(resumen["risk_category"]) == {"alto", "medio", "bajo", "no_churn"}
    assert resumen.notna().all().all()
    vacias = resumen[resumen["cuentas"] == 0]
    assert not vacias.empty
    assert (vacias["prob_promedio"] == 0).all()
