import math
from datetime import date, datetime, timedelta

import pytest

import pravin_options as O
from pravin_quant import IST, RiskPolicy


def test_textbook_values():
    # Hull, S=100 K=100 T=1 r=5% sigma=20%
    assert O.bs_price(100, 100, 1, 0.05, 0.2, 0, "CE") == pytest.approx(10.4506, abs=1e-4)
    assert O.bs_price(100, 100, 1, 0.05, 0.2, 0, "PE") == pytest.approx(5.5735, abs=1e-4)
    g = O.greeks(100, 100, 1, 0.05, 0.2, 0, "CE")
    assert g.delta == pytest.approx(0.6368, abs=1e-4)
    assert g.gamma == pytest.approx(0.018762, abs=1e-6)
    assert g.vega_per_vol_pt == pytest.approx(0.37524, abs=1e-5)
    assert g.theta_per_day == pytest.approx(-6.4140 / 365, abs=1e-5)


@pytest.mark.parametrize("S,K,T,q", [(25000, 25200, 7 / 365, 0.0), (51000, 50500, 0.5 / 365, 0.01), (100, 80, 2, 0.03)])
def test_put_call_parity_and_greeks_vs_finite_differences(S, K, T, q):
    r, s = 0.065, 0.14
    c, p = O.bs_price(S, K, T, r, s, q, "CE"), O.bs_price(S, K, T, r, s, q, "PE")
    assert c - p == pytest.approx(S * math.exp(-q * T) - K * math.exp(-r * T), rel=1e-9, abs=1e-8)
    for kind in ("CE", "PE"):
        g = O.greeks(S, K, T, r, s, q, kind)
        h = S * 1e-6
        fd_delta = (O.bs_price(S + h, K, T, r, s, q, kind) - O.bs_price(S - h, K, T, r, s, q, kind)) / (2 * h)
        assert g.delta == pytest.approx(fd_delta, rel=1e-4, abs=1e-6)
        dt = 1 / 365
        if T > dt:
            fd_theta = O.bs_price(S, K, T - dt, r, s, q, kind) - O.bs_price(S, K, T, r, s, q, kind)
            assert g.theta_per_day == pytest.approx(fd_theta, rel=0.05, abs=0.05)


def test_implied_vol_round_trip_and_bounds():
    for s in (0.08, 0.15, 0.4, 1.2):
        px = O.bs_price(25000, 25100, 5 / 365, 0.065, s, 0, "CE")
        assert O.implied_vol(px, 25000, 25100, 5 / 365, 0.065, 0, "CE") == pytest.approx(s, abs=1e-5)
    assert O.implied_vol(0.0001, 25000, 24000, 5 / 365, 0.065, 0, "CE") is None      # below intrinsic
    assert O.implied_vol(26000, 25000, 24000, 5 / 365, 0.065, 0, "CE") is None       # above spot


def test_year_fraction_counts_to_1530_ist():
    now = datetime(2026, 11, 17, 14, 30, tzinfo=IST)
    assert O.year_fraction(now, date(2026, 11, 17)) == pytest.approx(60 / O.MIN_PER_YEAR)
    assert O.year_fraction(now + timedelta(hours=2), date(2026, 11, 17)) == 0


def make_chain(spot, T, sigma=0.13, r=0.065, step=50, n=8, spread=0.01, kind="CE"):
    rows = []
    for k in range(-n, n + 1):
        K = round(spot / step) * step + k * step
        mid = O.bs_price(spot, K, T, r, sigma, 0, kind)
        half = max(mid * spread / 2, 0.05)
        rows.append({"strike": K, "type": kind, "bid": round(mid - half, 2), "ask": round(mid + half, 2), "oi": 1_000_000})
    return rows


NOW = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
EXP = date(2026, 11, 17)


def ev(chain, **kw):
    args = dict(spot=25000, direction=1, target=25150, stop=24940, minutes=90, now=NOW, expiry=EXP, lot_size=65,
                policy=RiskPolicy(capital=1_000_000), chain_time=NOW)
    args.update(kw)
    return O.evaluate_chain(chain, **args)


def test_ranks_liquid_near_money_calls_and_excludes_far_otm():
    T = O.year_fraction(NOW, EXP)
    res = ev(make_chain(25000, T))
    el = [c for c in res["candidates"] if c["eligible"]]
    assert res["status"] == "ok" and el
    assert all(0.35 <= c["delta"] <= 0.75 for c in el)
    far = [c for c in res["candidates"] if c["strike"] >= 25350]
    assert far and all(not c["eligible"] for c in far)
    assert el == sorted(el, key=lambda c: -c["option_rr"])
    best = el[0]
    assert best["premium_at_stop"] < best["ask"] < best["premium_at_target"]
    assert 25000 < best["breakeven_underlying"] < 25150


def test_wide_spread_and_small_budget_mean_no_trade():
    T = O.year_fraction(NOW, EXP)
    wide = ev(make_chain(25000, T, spread=0.2))
    assert wide["status"] == "no_trade"
    assert all(any("spread" in r for r in c["reasons"]) for c in wide["candidates"])
    poor = ev(make_chain(25000, T), policy=RiskPolicy(capital=20_000))       # ₹200 budget
    assert poor["status"] == "no_trade" and any("one lot risks" in r for c in poor["candidates"] for r in c["reasons"])


def test_puts_for_short_direction():
    T = O.year_fraction(NOW, EXP)
    res = ev(make_chain(25000, T, kind="PE"), direction=-1, target=24850, stop=25060)
    el = [c for c in res["candidates"] if c["eligible"]]
    assert el and all(c["kind"] == "PE" and c["delta"] < 0 for c in el)


def test_stale_chain_and_bad_plan_are_refused():
    T = O.year_fraction(NOW, EXP)
    assert ev(make_chain(25000, T), chain_time=NOW - timedelta(minutes=5))["status"] == "refused"
    assert ev(make_chain(25000, T), target=24900)["status"] == "refused"


def test_expiry_day_warning_and_decay_hurts_long_horizon():
    exp_day = datetime(2026, 11, 17, 11, 0, tzinfo=IST)
    T = O.year_fraction(exp_day, EXP)
    res = ev(make_chain(25000, T, sigma=0.15), now=exp_day, chain_time=exp_day, minutes=180)
    assert any("Expiry day" in w for w in res["warnings"])
    quick = ev(make_chain(25000, T, sigma=0.15), now=exp_day, chain_time=exp_day, minutes=15)
    k = lambda r: {c["strike"]: c["premium_at_target"] for c in r["candidates"] if c["premium_at_target"]}
    a, b = k(res), k(quick)
    common = set(a) & set(b)
    assert common and all(a[s] < b[s] for s in common)      # same move, more time -> less premium


def test_bad_quotes_excluded():
    T = O.year_fraction(NOW, EXP)
    ch = make_chain(25000, T)
    ch[8]["bid"] = 0
    ch[9]["ask"], ch[9]["bid"] = 1.0, 0.9                    # far below intrinsic/model bounds
    res = ev(ch)
    bad = {c["strike"]: c["reasons"] for c in res["candidates"] if not c["eligible"]}
    assert "no valid two-sided quote" in bad[ch[8]["strike"]]
