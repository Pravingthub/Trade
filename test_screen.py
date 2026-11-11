import numpy as np
import pandas as pd

import pravin_screen as SC

CFG = {"min_price": 100, "min_median_turnover_cr": 50, "top_n": 3, "history_days": 300}


def candles(n=300, start=500.0, drift=0.0, vol=1.0, volume=2_000_000, seed=0, last_narrow=False):
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(drift, vol / 100, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 200, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 200, n)))
    df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": float(volume)},
                      index=pd.bdate_range("2025-09-01", periods=n))
    if last_narrow:                                  # close exactly mid-range -> zero-width CPR
        m = (df["high"].iloc[-1] + df["low"].iloc[-1]) / 2
        df.iloc[-1, df.columns.get_loc("close")] = m
    return df


def test_universe_keeps_eq_only_and_marks_options():
    nse = [{"tradingsymbol": "ABC", "instrument_type": "EQ", "segment": "NSE", "instrument_token": 1, "name": "ABC LTD"},
           {"tradingsymbol": "XYZ-BE", "instrument_type": "EQ", "segment": "NSE", "instrument_token": 2},
           {"tradingsymbol": "NIFTY 50", "instrument_type": "EQ", "segment": "INDICES", "instrument_token": 3}]
    nfo = [{"name": "ABC", "instrument_type": "CE", "segment": "NFO-OPT", "lot_size": 500}]
    u = SC.universe_from_instruments(nse, nfo)
    assert [x["symbol"] for x in u] == ["ABC"] and u[0]["has_options"] and u[0]["lot_size"] == 500


def test_illiquid_and_cheap_stocks_are_dropped():
    sym = {"symbol": "S", "name": "S", "token": 1, "has_options": False, "lot_size": None}
    assert SC.assess(sym, candles(volume=1_000), 0.0, CFG) is None              # turnover far too low
    assert SC.assess(sym, candles(start=20.0, volume=10**9), 0.0, CFG) is None  # price below 100
    assert SC.assess(sym, candles(), 0.0, CFG) is not None


def test_ranking_prefers_narrow_cpr_and_relative_strength():
    uni = [{"symbol": f"S{k}", "name": f"S{k}", "token": k, "has_options": k == 0, "lot_size": 400 if k == 0 else None}
           for k in range(12)]
    hist = {f"S{k}": candles(seed=k, drift=0.004 if k == 0 else 0.0) for k in range(12)}
    hist["S0"] = candles(seed=0, drift=0.01, vol=2.0, last_narrow=True)
    hist["S11"] = candles(seed=11, volume=100)                                   # illiquid
    res = SC.run(uni, hist, candles(seed=99), CFG, risk_budget=1000)
    assert res["tradable"] == 11 and res["dropped"] == 1
    syms = {t["symbol"]: t for t in res["top"]}
    assert "S0" in syms and any("narrow CPR" in w for w in syms["S0"]["reasons"])
    assert [t["points"] for t in res["top"]] == sorted((t["points"] for t in res["top"]), reverse=True)
    assert len(res["top"]) == 3 and "S11" not in syms
    assert SC.ordinal(2) == "2nd" and SC.ordinal(11) == "11th" and SC.ordinal(23) == "23rd"


def test_plan_sizes_shares_and_refuses_oversized_options():
    r = {"cpr_top": 510.0, "cpr_bottom": 508.0, "atr": 12.0, "has_options": True, "lot_size": 1000}
    p = SC.plan(r, risk_budget=1000)
    assert p["long"]["stop"] == 507.0 and p["long"]["target"] == 516.0          # buffer = max(2, 3) = 3
    assert p["long"]["shares"] == 333 and p["long"]["share_risk"] <= 1000
    assert "trade the shares" in p["long"]["options"]                            # 0.6*3*1000 = 1800 > 1000
    small = SC.plan({**r, "lot_size": 400}, 1000)
    assert small["short"]["options"].startswith("options fit")                   # 0.6*3*400 = 720


def test_results_today_flagged_as_event_risk():
    uni = [{"symbol": "ABC", "name": "ABC Ltd", "token": 1, "has_options": False, "lot_size": None}]
    res = SC.run(uni, {"ABC": candles()}, candles(seed=5), {**CFG, "top_n": 1}, 1000,
                 [{"title": "ABC Ltd - Financial Results for the quarter"}])
    assert res["top"][0]["event_risk"] and res["top"][0]["news"]
