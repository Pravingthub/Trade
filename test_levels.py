import json

import pytest

from conftest import FIXTURES
from pravin_quant import (
    CPR,
    CPRRelation,
    InvalidOHLC,
    camarilla,
    cpr,
    cpr_relation,
    floor_pivots,
    is_virgin,
    price_position,
)

CASES = json.loads((FIXTURES / "levels_reference.json").read_text())["cases"]
TOL = 1e-6


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_cpr_matches_hand_calculation(case):
    got = cpr(case["high"], case["low"], case["close"]).to_dict()
    for k, v in case["cpr"].items():
        assert got[k] == pytest.approx(v, abs=TOL), k


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_floor_pivots_match_hand_calculation(case):
    got = floor_pivots(case["high"], case["low"], case["close"]).to_dict()
    for k, v in case["floor"].items():
        assert got[k] == pytest.approx(v, abs=TOL), k


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_camarilla_matches_hand_calculation(case):
    got = camarilla(case["high"], case["low"], case["close"]).to_dict()
    for k, v in case["camarilla"].items():
        assert got[k] == pytest.approx(v, abs=TOL), k


def test_raw_tc_bc_kept_and_top_bottom_normalised():
    lvl = cpr(100, 90, 91)
    assert lvl.tc < lvl.bc                      # raw values preserved
    assert lvl.top == lvl.bc and lvl.bottom == lvl.tc


def test_camarilla_multiplier_is_configurable():
    a, b = camarilla(110, 100, 105, 1.1), camarilla(110, 100, 105, 1.0)
    assert a.r4 - 105 == pytest.approx(5.5) and b.r4 - 105 == pytest.approx(5.0)


@pytest.mark.parametrize("h,l,c", [(90, 100, 95), (100, 90, 101), (100, 90, 0),
                                   (100, float("nan"), 95), (-1, -5, -2)])
def test_invalid_inputs_rejected(h, l, c):
    with pytest.raises(InvalidOHLC):
        cpr(h, l, c)


def _lvl(top, bottom):
    p = (top + bottom) / 2
    return CPR(p, bottom, top, top, bottom, top - bottom, 0)


@pytest.mark.parametrize("prev,cur,expected", [
    ((110, 100), (120, 115), CPRRelation.HIGHER_VALUE),
    ((110, 100), (95, 90), CPRRelation.LOWER_VALUE),
    ((110, 100), (108, 102), CPRRelation.INSIDE),
    ((110, 100), (112, 98), CPRRelation.OUTSIDE),
    ((110, 100), (114, 105), CPRRelation.OVERLAPPING_HIGHER),
    ((110, 100), (105, 96), CPRRelation.OVERLAPPING_LOWER),
    ((110, 100), (110, 100), CPRRelation.UNCHANGED),
])
def test_cpr_relation(prev, cur, expected):
    assert cpr_relation(_lvl(*prev), _lvl(*cur)) is expected


def test_virgin_and_position():
    lvl = _lvl(110, 100)
    assert is_virgin(lvl, session_high=130, session_low=111)
    assert not is_virgin(lvl, session_high=130, session_low=109)
    assert price_position(111, lvl) == "above"
    assert price_position(105, lvl) == "inside"
    assert price_position(99, lvl) == "below"
