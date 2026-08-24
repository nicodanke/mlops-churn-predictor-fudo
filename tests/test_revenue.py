"""Revenue mensual: la suma de plan base + modulos en el mercado de la cuenta."""

import pytest

from churn.pricing.revenue import PricingBook, revenue_for_plan


@pytest.fixture
def book():
    return PricingBook(
        currency="USD",
        plans={"adv": {"mx": 70.0, "ar": 50.0}, "pro": {"mx": 95.0}},
        modules={"tbl": {"mx": 15.0, "ar": 10.0}, "kds": {"mx": None}},
    )


def test_suma_plan_base_y_modulos(book):
    resultado = revenue_for_plan("adv-tbl-mx", book)
    assert resultado.total == 85.0
    assert resultado.is_complete
    assert resultado.market == "mx"


def test_usa_el_precio_del_mercado_correcto(book):
    assert revenue_for_plan("adv-tbl-ar", book).total == 60.0
    assert revenue_for_plan("adv-tbl-mx", book).total == 85.0


def test_marca_los_precios_faltantes_sin_romper(book):
    resultado = revenue_for_plan("adv-kds-mx", book)
    assert resultado.total == 70.0  # solo suma lo que tiene precio
    assert not resultado.is_complete
    assert "module:kds@mx" in resultado.missing_prices


def test_plan_sin_precio_cargado_no_rompe(book):
    resultado = revenue_for_plan("ini-cl", book)
    assert resultado.total == 0.0
    assert not resultado.is_complete


def test_desglose_lista_cada_item(book):
    items = revenue_for_plan("adv-tbl-mx", book).items
    assert items == [
        {"kind": "plan", "code": "adv", "price": 70.0},
        {"kind": "module", "code": "tbl", "price": 15.0},
    ]


def test_is_filled_detecta_lista_vacia():
    vacia = PricingBook(currency="USD", plans={"adv": {"ar": None}}, modules={})
    assert not vacia.is_filled
    cargada = PricingBook(currency="USD", plans={"adv": {"ar": 10.0}}, modules={})
    assert cargada.is_filled
