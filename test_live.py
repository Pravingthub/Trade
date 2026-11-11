from datetime import datetime, timedelta

import pytest

import pravin_backtest as B
import pravin_live as L
from pravin_quant import IST, ClosedTrade, DailyGuard, RiskPolicy
from test_backtest import synthetic_market

T0 = datetime(2026, 6, 2, 9, 15, tzinfo=IST)


def at(minutes, seconds=0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def test_candle_builder_buckets_and_ignores_outside_hours():
    cb = L.CandleBuilder()
    assert cb.on_tick(T0 - timedelta(minutes=5), 100) == []          # pre-open ignored
    cb.on_tick(at(0, 1), 100)
    cb.on_tick(at(2), 105)
    cb.on_tick(at(4, 59), 98)
    bars = cb.on_tick(at(5, 1), 99)
    assert bars == [{"ts": T0, "open": 100, "high": 105, "low": 98, "close": 98, "ticks": 3}]
    assert cb.flush(at(9)) == [] and len(cb.flush(at(10))) == 1


def test_candle_builder_matches_backtest_bar_convention():
    cb = L.CandleBuilder()
    for k in range(30):
        cb.on_tick(at(k), 100 + k)
    cb.flush(at(31))
    f = cb.frame()
    assert list(f.index[:2]) == [T0, T0 + timedelta(minutes=5)]
    assert f["open"].iloc[1] == 105 and f["close"].iloc[1] == 109


@pytest.fixture
def watcher():
    daily, _ = synthetic_market(n_days=300, seed=5)
    daily = B._date_index(daily)
    day = daily.index[-1]
    w = L.Watcher("NIFTY50", "NIFTY 50", daily.iloc[:-1], day, validated=set(),
                  guard=DailyGuard(RiskPolicy(capital=100_000)), lot_size=65)
    return w


def day_t(w, minutes, seconds=0):
    return datetime(w.day.year, w.day.month, w.day.day, 9, 15, tzinfo=IST) + timedelta(minutes=minutes, seconds=seconds)


def feed(w, prices):
    out = []
    for k, p in enumerate(prices):
        out += w.on_tick(day_t(w, 5 * k, 30), p)
    out += w.check_clock(day_t(w, 5 * len(prices) + 1))
    return out


def test_level_cross_alerts_fire_once(watcher):
    top = watcher.levels["CPR top"]
    alerts = feed(watcher, [top - 20, top + 5, top - 3, top + 6])
    ups = [a for a in alerts if a.key == "CPR top-up"]
    assert len(ups) == 1 and "above CPR top" in ups[0].text


def test_stale_feed_pauses_and_resumes(watcher):
    watcher.on_tick(day_t(watcher, 30), 100)
    a = watcher.check_clock(day_t(watcher, 32))
    assert watcher.stale and a[-1].kind == "feed" and "paused" in a[-1].text
    b = watcher.on_tick(day_t(watcher, 33), 101)
    assert not watcher.stale and "back" in b[0].text


def narrow_long_prices(w):
    c = w.ctx.cpr
    return [c.top + 15, c.top + 16, c.top + 14, c.top + 40, c.top + 41, c.top + 42]


def test_unvalidated_setups_are_silent_by_default(watcher):
    watcher.ctx.width_pct_rank = 5
    alerts = feed(watcher, narrow_long_prices(watcher))
    assert not [a for a in alerts if a.kind == "setup"]


def test_validated_setup_alert_includes_stop_target_and_size(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    alerts = [a for a in feed(watcher, narrow_long_prices(watcher)) if a.kind == "setup"]
    assert len(alerts) == 1
    t = alerts[0].text
    assert "LONG" in t and "Stop" in t and "target" in t and ("lot" in t or "NO TRADE" in t)
    assert "NOT VALIDATED" not in t


def test_unvalidated_shown_only_when_asked_and_labelled(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.show_unvalidated = True
    alerts = [a for a in feed(watcher, narrow_long_prices(watcher)) if a.kind == "setup"]
    assert alerts and alerts[0].text.split(": ", 1)[1].startswith("NOT VALIDATED")


def test_risk_guard_is_shown_on_setup_alerts(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    t = day_t(watcher, 0)
    watcher.guard.trades = [ClosedTrade(t, -300), ClosedTrade(t + timedelta(minutes=1), -300)]
    alerts = [a for a in feed(watcher, narrow_long_prices(watcher)) if a.kind == "setup"]
    assert alerts and "RISK GUARD" in alerts[0].text


def test_live_and_backtest_agree_on_the_same_bars(watcher):
    """The live watcher must find the same signal the backtester would."""
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    feed(watcher, narrow_long_prices(watcher))
    bars = watcher.builder.frame()
    ctx = B.SessionContext(watcher.day, bars, watcher.ctx.prev, watcher.ctx.cpr, watcher.ctx.pivots,
                           "narrow", 5, watcher.ctx.weekly_cpr, watcher.ctx.regime)
    s = B.get_strategy("CPR-NARROW-OPEN")
    assert s.find(ctx, s.params).signal_bar == 3


def test_live_module_cannot_place_orders():
    src = open(L.__file__).read()
    for word in ("place_order", "modify_order", "cancel_order", "place_gtt"):
        assert word not in src
