"""Parseo de plan codes: es lo que alimenta el calculo de revenue."""

import pytest

from churn.pricing.plans import parse_plan


@pytest.mark.parametrize(
    ("code", "base", "modules", "market"),
    [
        ("adv-tbl-mx", "adv", ("tbl",), "mx"),
        ("ba-cl", "ba", (), "cl"),
        ("pro-idvsls-kds-fi-tbl-ar", "pro", ("idvsls", "kds", "fi", "tbl"), "ar"),
        ("fu-fe-kds-idvsls-cl", "fu", ("fe", "kds", "idvsls"), "cl"),
        ("st-fi-dv-ar", "st", ("fi", "dv"), "ar"),
        ("fu-int", "fu", (), "int"),
    ],
)
def test_parse_plan_descompone_correctamente(code, base, modules, market):
    parsed = parse_plan(code)
    assert parsed.base == base
    assert parsed.modules == modules
    assert parsed.market == market
    assert parsed.n_modules == len(modules)


def test_parse_plan_tolera_tokens_desconocidos():
    """Un modulo nuevo no debe romper el pipeline, solo quedar registrado."""
    parsed = parse_plan("adv-nuevomodulo-tbl-ar")
    assert parsed.base == "adv"
    assert parsed.modules == ("tbl",)
    assert parsed.unknown_tokens == ("nuevomodulo",)


def test_parse_plan_tolera_entrada_vacia():
    for value in (None, "", "   "):
        parsed = parse_plan(value)
        assert parsed.base is None
        assert parsed.modules == ()


def test_rank_ordena_los_planes_comercialmente():
    assert parse_plan("ini-ar").rank < parse_plan("ba-ar").rank
    assert parse_plan("ba-ar").rank < parse_plan("adv-ar").rank
    assert parse_plan("adv-ar").rank < parse_plan("fu-ar").rank


def test_describe_es_legible():
    assert parse_plan("adv-tbl-mx").describe() == "Plan Avanzado + Mesas (Mexico)"
