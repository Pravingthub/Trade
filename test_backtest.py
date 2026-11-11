"""Backtester tests: hand-checked charges, fills, ambiguity, no look-ahead,
metrics, splits and walk-forward."""
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import pravin_backtest as B
from pravin_quant import IST, cpr

# ----------------------------------------------------------------- charges


def test_futures_charges_by_hand_post_april_2026():
    # 1 lot-equivalent of 65 units: buy at 25,000, sell at 25,100 on 2026-06-01
    buy, sell = 25000 * 65, 25100 * 65                 # 1,625,000 and 1,631,500
    c = B.futures_charges(buy, sell, date(2026, 6, 1))
    assert c["brokerage"] == pytest.approx(40.0)        # 0.03% > ₹20 on both legs
    assert c["stt"] == pytest.approx(0.0005 * 1631500)  # ₹815.75
    assert c["exchange"] == pytest.approx(0.0000173 * 3256500)
    assert c["stamp"] == pytest.approx(0.00002 * 1625000)
    assert c["gst"] == pytest.approx(0.18 * (40 + 0.0000173 * 3256500 + 0.000001 * 3256500))
    assert c["total"] == pytest.approx(sum(v for k, v in c.items() if k != "total"))


@pytest.mark.parametrize("d,stt", [(date(2024, 9, 30), 0.000125), (date(2024, 10, 1), 0.0002),
                                   (date(2026, 3, 31), 0.0002), (date(2026, 4, 1), 0.0005)])
def test_stt_schedule_changes_on_the_right_dates(d, stt):
    assert B.rates_on(d).futures_stt_sell == stt


def test_options_charges_flat_brokerage_and_stt_on_sell_premium():
    c = B.options_charges(100 * 65, 130 * 65, date(2026, 6, 1))
    assert c["brokerage"] == 40 and c["stt"] == pytest.approx(0.0015 * 130 * 65)


def test_small_trade_brokerage_is_percentage_not_flat():
    c = B.futures_charges(10000, 10000, date(2026, 6, 1))
    assert c["brokerage"] == pytest.approx(6.0)          # 0.03% of 10k = ₹3 per leg


# ----------------------------------------------------------------- synthetic market


def bars_for(day, path):
    """path: list of (open, high, low, close) for consecutive 5-minute bars from 09:15."""
    idx = [datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * k) for k in range(len(path))]
    return pd.DataFrame(path, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx)).assign(volume=0.0)


def flat_session(day, px, n=75):
    return bars_for(day, [(px, px + 1, px - 1, px)] * n)


def ctx(day, bars, prev=(1010, 990, 1001), width_rank=10.0, weekly=None):
    lvl = cpr(*prev)
    return B.SessionContext(day, bars, {"high": prev[0], "low": prev[1], "close": prev[2]}, lvl, None,
                            "narrow" if width_rank <= 20 else "normal", width_rank, weekly, "trending_bullish")


D = date(2026, 6, 2)
LVL = cpr(1010, 990, 1001)   # P=1000.333 BC=1000 TC=1000.667 -> top 1000.667, bottom 1000


def long_day(after_entry):
    """Opens above CPR, confirms above OR high at 09:30 bar close, then follows `after_entry` bars."""
    pre = [(1003, 1004, 1002, 1003)] * 3 + [(1003, 1006, 1003, 1005.5)]   # 09:15-09:30, signal on 09:30 bar
    return bars_for(D, pre + after_entry + [(1010, 1011, 1009, 1010)] * (75 - len(pre) - len(after_entry)))


def test_narrow_open_long_hits_target_with_next_bar_entry():
    b = long_day([(1006, 1007, 1005, 1006)] + [(1006, 1020, 1006, 1019)])
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 2.0})
    assert s.direction == 1 and s.signal_bar == 3 and s.stop == pytest.approx(LVL.bottom)
    t = B.simulate(ctx(D, b), s, "X", qty=10, slippage=0.0)
    assert t.entry == 1006                                # NEXT bar's open, not the signal close
    assert t.exit_reason == "target" and t.exit == pytest.approx(s.target)
    assert t.r_gross == pytest.approx((s.target - 1006) / (1006 - LVL.bottom))


def test_stop_assumed_first_when_bar_spans_both():
    b = long_day([(1006, 1030, 990, 1010)])
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 2.0})
    t = B.simulate(ctx(D, b), s, "X", qty=1, slippage=0.0)
    assert t.exit_reason == "stop" and t.exit == pytest.approx(LVL.bottom)


def test_gap_through_stop_fills_at_open_not_stop():
    b = long_day([(1006, 1007, 1005, 1006), (995, 996, 990, 992)])
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 2.0})
    t = B.simulate(ctx(D, b), s, "X", qty=1, slippage=0.0)
    assert t.exit_reason == "stop" and t.exit == 995


def test_time_exit_at_1515():
    b = long_day([(1006, 1007, 1005, 1006)])
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 10.0})
    t = B.simulate(ctx(D, b), s, "X", qty=1, slippage=0.0)
    assert t.exit_reason == "time" and t.exit_time.endswith("15:15:00+05:30")


def test_slippage_is_against_us_both_sides():
    b = long_day([(1006, 1007, 1005, 1006)])
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 10.0})
    t0 = B.simulate(ctx(D, b), s, "X", 1, 0.0)
    t1 = B.simulate(ctx(D, b), s, "X", 1, 2.0)
    assert t1.entry == t0.entry + 2 and t1.exit == t0.exit - 2


def test_not_narrow_means_no_setup():
    b = long_day([])
    assert B.find_narrow_open_beyond(ctx(D, b, width_rank=55), {"narrow_pct": 20, "rr": 2}) is None


def test_no_signal_in_first_15_minutes():
    b = bars_for(D, [(1003, 1008, 1003, 1007)] + [(1003, 1004, 1002, 1003)] * 74)
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 2})
    assert s is None


def test_short_side_mirror():
    pre = [(997, 998, 996, 997)] * 3 + [(997, 997, 994, 994.5)]
    b = bars_for(D, pre + [(994, 995, 980, 981)] + [(981, 982, 980, 981)] * 70)
    s = B.find_narrow_open_beyond(ctx(D, b), {"narrow_pct": 20, "rr": 2.0})
    assert s.direction == -1 and s.stop == pytest.approx(LVL.top)
    t = B.simulate(ctx(D, b), s, "X", 1, 0.0)
    assert t.exit_reason == "target" and t.gross > 0


def test_inside_breakout_and_rejection_finders():
    inside = bars_for(D, [(1000.3, 1000.6, 1000.1, 1000.4)] * 3 + [(1000.4, 1003, 1000.3, 1002)] + [(1002, 1003, 1001, 1002)] * 71)
    s = B.find_inside_breakout(ctx(D, inside), {"rr": 2.0})
    assert s.direction == 1 and s.signal_bar == 3
    rej = bars_for(D, [(1005, 1006, 1004, 1005)] * 3 + [(1005, 1005, 1000.2, 1003)] + [(1003, 1004, 1002, 1003)] * 71)
    s2 = B.find_cpr_rejection(ctx(D, rej), {"rr": 2.0, "buffer_pct": 0.05})
    assert s2.direction == 1 and s2.stop < LVL.bottom


# ----------------------------------------------------------------- contexts / look-ahead


def synthetic_market(n_days=420, seed=3):
    rng = np.random.default_rng(seed)
    days = [d.date() for d in pd.bdate_range("2024-01-01", periods=n_days)]
    frames, px = [], 20000.0
    for d in days:
        steps = rng.normal(0, 8, 75)
        closes = px + np.cumsum(steps)
        opens = np.concatenate([[px + rng.normal(0, 20)], closes[:-1]])
        hi = np.maximum(opens, closes) + rng.uniform(0, 6, 75)
        lo = np.minimum(opens, closes) - rng.uniform(0, 6, 75)
        idx = [datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * k) for k in range(75)]
        frames.append(pd.DataFrame({"open": opens, "high": hi, "low": lo, "close": closes, "volume": 0.0}, index=idx))
        px = closes[-1]
    intra = pd.concat(frames)
    g = intra.groupby(intra.index.tz_convert(IST).date)
    daily = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(), "volume": 0.0})
    return daily, intra


DAILY, INTRA = synthetic_market()


def test_contexts_use_previous_day_only():
    cs = B.build_contexts(DAILY, INTRA, warmup=260)
    c = cs[10]
    i = list(DAILY.index).index(c.day)
    p = DAILY.iloc[i - 1]
    assert c.cpr.pivot == pytest.approx((p.high + p.low + p.close) / 3)
    # poison everything from the session onward: context must not change
    poisoned_d = DAILY.copy()
    poisoned_d.iloc[i:, :4] = 1.0
    cs2 = B.build_contexts(poisoned_d, INTRA, warmup=260)
    c2 = next(x for x in cs2 if x.day == c.day)
    assert c2.cpr == c.cpr and c2.width_pct_rank == c.width_pct_rank and c2.regime == c.regime


def test_weekly_cpr_comes_from_completed_week():
    cs = B.build_contexts(DAILY, INTRA, warmup=260)
    c = next(x for x in cs if x.day.weekday() == 2)          # a Wednesday
    monday = c.day - timedelta(days=2)
    prev_week = DAILY[(DAILY.index >= monday - timedelta(days=7)) & (DAILY.index < monday)]
    w = cpr(prev_week.high.max(), prev_week.low.min(), prev_week.close.iloc[-1])
    assert c.weekly_cpr == w


# ----------------------------------------------------------------- metrics & validation


def T(net, r, d=date(2026, 1, 5)):
    return B.Trade("X", d, 1, "", 0, 0, 0, "", 0, "target", 1, 1, net + 50, 50, net, r, r, "r", "n")


def test_metrics_by_hand():
    trades = [T(100, 1), T(-50, -0.5), T(-50, -0.5), T(200, 2), T(-100, -1)]
    m = B.metrics(trades, sessions=10)
    assert m["trades"] == 5 and m["win_rate_pct"] == 40
    assert m["net"] == 100 and m["profit_factor"] == pytest.approx(300 / 200)
    assert m["max_drawdown_rs"] == 100                     # 100 -> 0 peak-to-trough
    assert m["max_consecutive_losses"] == 2
    assert m["expectancy_r"] == pytest.approx(0.2)
    assert m["charges"] == 250


def test_bootstrap_ci_needs_sample():
    assert B.bootstrap_ci([1.0] * 10) is None
    lo, hi = B.bootstrap_ci(list(np.random.default_rng(1).normal(0.3, 1, 200)))
    assert lo < 0.3 < hi


def test_split_is_chronological_and_holdout_hidden():
    cs = B.build_contexts(DAILY, INTRA, warmup=60)
    a, b, c = B.split(cs)
    assert a[-1].day < b[0].day and b[-1].day < c[0].day
    res = B.evaluate(B.STRATEGIES[0], cs, qty=50)
    assert res["holdout"] == "hidden"
    assert B.evaluate(B.STRATEGIES[0], cs, qty=50, reveal_holdout=True)["holdout"] != "hidden"


def test_verdict_gate():
    good = {"trades": 40, "profit_factor": 1.5, "expectancy_r": 0.3, "expectancy_r_ci95": (0.05, 0.5)}
    assert B.verdict(good, {"trades": 40, "expectancy_r": 0.1})["status"] == "candidate_for_paper_trading"
    assert B.verdict({**good, "expectancy_r_ci95": (-0.1, 0.5)}, {"trades": 40, "expectancy_r": 0.1})["status"] == "rejected"
    assert B.verdict({**good, "trades": 12}, {})["status"] == "insufficient_sample"


def test_walk_forward_only_reports_unseen_windows():
    cs = B.build_contexts(DAILY, INTRA, warmup=60)
    wf = B.walk_forward(B.get_strategy("CPR-INSIDE-BREAK"), cs, qty=50, slippage=1.0, train=120, test=40, min_trades=3)
    for w, start in zip(wf["windows"], range(120, len(cs), 40)):
        assert w["test_from"] == str(cs[start].day)
    days = {t.day for t in wf["trades"]}
    assert all(d >= cs[120].day for d in days)


def test_random_walk_market_has_no_edge_after_costs():
    """Sanity check against self-deception: on a pure random walk, no CPR setup
    should look like a winner after costs."""
    cs = B.build_contexts(DAILY, INTRA, warmup=60)
    for s in B.STRATEGIES:
        m = B.metrics(B.run(s, cs, qty=50, slippage=1.0), len(cs))
        if m["trades"] >= 30:
            assert B.verdict(m, m)["status"] != "candidate_for_paper_trading", s.id


def test_full_report_runs_and_is_json_serialisable():
    import json
    d, i = synthetic_market(n_days=600, seed=11)
    rep = B.backtest_instrument(d, i, qty=50)
    json.dumps(rep, default=str)
    assert {r["strategy"]["id"] for r in rep["results"]} == {s.id for s in B.STRATEGIES}
    assert "| Setup |" in B.to_markdown("TEST", rep)
