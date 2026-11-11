from datetime import datetime, timedelta

import pytest

from pravin_quant import ClosedTrade, DailyGuard, RiskPolicy, position_size, reward_risk, stop_discipline

POLICY = RiskPolicy(capital=100_000)          # 1% = ₹1,000 per trade
T0 = datetime(2026, 2, 2, 10, 0)


def test_equity_sizing():
    r = position_size(POLICY, entry=2500, stop=2480)           # ₹20/share
    assert (r.ok, r.quantity, r.rupee_risk) == (True, 50, 1000)


def test_option_lot_larger_than_budget_is_no_trade():
    r = position_size(POLICY, entry=120, stop=100, lot_size=75)  # one lot = ₹1,500
    assert not r.ok and r.quantity == 0 and "one lot" in r.reason


def test_option_whole_lots_only():
    r = position_size(POLICY, entry=120, stop=114, lot_size=75)  # ₹450/lot -> 2 lots
    assert (r.lots, r.quantity, r.rupee_risk) == (2, 150, 900)


def test_short_and_wrong_side_stop():
    assert position_size(POLICY, 100, 102, direction="short").quantity == 500
    assert not position_size(POLICY, 100, 102, direction="long").ok


def test_reward_risk():
    assert reward_risk(100, 95, 112) == pytest.approx(2.4)
    assert reward_risk(100, 105, 90, "short") == pytest.approx(2.0)


def test_guard_trade_limit():
    g = DailyGuard(POLICY, opened_today=3)
    s = g.status(T0)
    assert not s["can_recommend"] and "limit" in s["reasons"][0]


def test_guard_cooldown_after_loss_then_release():
    g = DailyGuard(POLICY, [ClosedTrade(T0, -400)], opened_today=1)
    assert not g.status(T0 + timedelta(minutes=10))["can_recommend"]
    assert g.status(T0 + timedelta(minutes=31))["can_recommend"]


def test_guard_two_consecutive_losses_ends_day():
    g = DailyGuard(POLICY, [ClosedTrade(T0, -300), ClosedTrade(T0 + timedelta(hours=1), -300)],
                   opened_today=2)
    s = g.status(T0 + timedelta(hours=4))
    assert not s["can_recommend"] and any("in a row" in r for r in s["reasons"])


def test_guard_daily_loss_stop():
    g = DailyGuard(POLICY, [ClosedTrade(T0, -2_100)], opened_today=1)
    s = g.status(T0 + timedelta(hours=3))
    assert any("daily loss stop" in r for r in s["reasons"])


def test_guard_weekly_loss_stop_carries_across_days():
    mon = datetime(2026, 2, 2, 11)
    trades = [ClosedTrade(mon + timedelta(days=i), -1_300) for i in range(4)]
    s = DailyGuard(POLICY, trades).status(mon + timedelta(days=4, hours=1))
    assert any("weekly" in r for r in s["reasons"])


def test_new_day_resets_daily_limits():
    g = DailyGuard(POLICY, [ClosedTrade(T0, -2_500)], opened_today=0)
    assert g.status(T0 + timedelta(days=1))["can_recommend"]


def test_stop_discipline_measures_late_exits():
    trades = [ClosedTrade(T0, -1_600, planned_stop_loss=1_000),
              ClosedTrade(T0, -900, planned_stop_loss=1_000),
              ClosedTrade(T0, 500, planned_stop_loss=1_000)]
    assert stop_discipline(trades) == {"late_exits": 1, "extra_loss": 600}
