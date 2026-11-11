from datetime import datetime, timedelta

from pravin_quant import IST
from pravin_stance import StanceEngine

D = datetime(2026, 11, 13, 9, 15, tzinfo=IST)


def at(minutes):
    return D + timedelta(minutes=minutes)


def eng(**kw):
    return StanceEngine("NIFTY", cpr_top=100.0, cpr_bottom=99.0, **kw)


def test_open_sets_bias():
    e = eng()
    assert e.on_open(101, at(0)).new == "LONG_BIAS"
    e2 = eng()
    assert e2.on_open(98, at(0)).new == "SHORT_BIAS"
    e3 = eng()
    assert e3.on_open(99.5, at(0)) is None and e3.stance == "NEUTRAL"


def test_neutral_waits_until_0930_then_breaks():
    e = eng()
    e.on_open(99.5, at(0))
    assert e.on_bar(101, at(5)) is None                     # 09:20: too early
    assert e.on_bar(101, at(15)).new == "LONG_BIAS"         # 09:30


def test_failed_thesis_returns_to_neutral():
    e = eng()
    e.on_open(101, at(0))
    c = e.on_bar(98.5, at(40))
    assert c.new == "NEUTRAL" and "failed" in c.reason


def test_anti_whipsaw_gap_and_cap():
    e = eng(max_changes=2, min_gap=timedelta(minutes=30))
    e.on_open(101, at(0))
    assert e.on_bar(98.5, at(10)) is None                   # too soon after the last change
    assert e.on_bar(98.5, at(31)).new == "NEUTRAL"
    assert e.on_bar(101, at(70)) is None                    # cap of 2 discretionary changes reached
    assert e.stance == "NEUTRAL"


def test_protective_changes_are_never_blocked_and_done_is_final():
    e = eng(max_changes=1)
    e.on_open(101, at(0))
    assert e.on_news("high", "check", "RBI announces...", at(1)).new == "STAND_ASIDE"
    assert e.on_guard({"can_recommend": False, "reasons": ["2 losses in a row"]}, at(2)).new == "DONE_FOR_DAY"
    assert e.on_open(105, at(3)) is None and e.on_bar(105, at(60)) is None


def test_stand_aside_expires_then_reevaluates():
    e = eng()
    e.on_open(101, at(0))
    e.on_news("high", "check", "x", at(20))
    assert e.on_bar(102, at(30)) is None and e.stance == "STAND_ASIDE"
    c = e.on_bar(102, at(55))                               # 30 minutes later: re-evaluated
    assert c.new == "LONG_BIAS"


def test_news_against_bias_and_proxy_shock():
    e = eng()
    e.on_open(101, at(0))
    assert e.on_news("medium", "adverse", "RBI liquidity absorption", at(40)).new == "NEUTRAL"
    e2 = eng()
    e2.on_open(101, at(0))
    assert e2.on_proxy("Crude", 3.4, 3.0, at(60)).new == "STAND_ASIDE"
    assert e2.on_proxy("Crude", 1.0, 3.0, at(61)) is None


def test_premarket_and_cutoff():
    e = eng()
    c = e.premarket(guard_ok=True, event_today=False, global_shock=["S&P 500 -2.10%"], at=D.replace(hour=8, minute=50))
    assert c.new == "STAND_ASIDE" and "09:45" in c.reason
    assert e.on_bar(101, at(29)) is None                    # 09:44 still standing aside
    assert e.on_bar(101, at(31)).new == "LONG_BIAS"         # 09:46 re-evaluated
    assert e.on_bar(101, at(5 * 60 + 20)).new == "DONE_FOR_DAY"   # 14:35
