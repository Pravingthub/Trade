import pytest

import pravin_watch as Wt


def lvl(name, price, tf="D", kind="pivot"):
    return {"name": name, "price": price, "tf": tf, "kind": kind}


def test_zones_cluster_and_rank_confluence():
    levels = [lvl("D R1", 1010), lvl("W pivot", 1012, "W", "cpr"), lvl("M R1", 1011, "M"),   # one zone above
              lvl("D R2", 1040), lvl("D S1", 990), lvl("D Cam S3", 950, "D", "camarilla"), lvl("far", 2000)]
    z = Wt.zones(levels, close=1000, atr=50, tol_atr=0.08)               # tolerance 4 pts
    a = z["above"][0]
    assert (a["low"], a["high"]) == (1010, 1012) and a["confluence"] and a["timeframes"] == ["D", "W", "M"]
    assert a["strength"] == 5 and z["above"][1]["mid"] == 1040
    assert z["below"][0]["mid"] == 990 and all(x["mid"] != 2000 for x in z["above"])   # beyond 2.5 ATR dropped


def test_zone_at_price_detected():
    z = Wt.zones([lvl("a", 998), lvl("b", 1001)], close=1000, atr=50)
    assert z["at_price"] and not z["above"] and not z["below"]


def test_headline_reads_mixed_timeframes():
    cpr = {p: {"reference_price": {"position": pos}} for p, pos in (("day", "below"), ("week", "below"), ("month", "above"))}
    lines = Wt.headline(cpr, 100.0, [], [])
    assert "below the daily CPR" in lines[0] and "higher-timeframe uptrend" in lines[1]


def H(open_vs, close, top=101, bottom=99, high=105, low=95, width=0.5, virgin=False, rel="higher_value"):
    return {"open_vs_cpr": open_vs, "open": 100.0, "close": close, "cpr_top": top, "cpr_bottom": bottom, "high": high, "low": low,
            "width_pct": width, "virgin": virgin, "relation": rel}


def test_cpr_behaviour_counts_by_hand():
    rows = [H("above", 103)] * 8 + [H("above", 100)] * 2 + [H("below", 97)] * 5 + [H("below", 102)] * 5 \
        + [H("inside", 104)] * 6 + [H("inside", 100)] * 4
    b = Wt.cpr_behaviour(rows)
    t = [i["text"] for i in b["insights"]]
    assert any("Opened above the CPR on 10 days; closed above it 80%" in x for x in t)
    assert any("Opened below the CPR on 10 days; closed below it 50%" in x for x in t)
    assert any("Opened inside the CPR on 10 days; closed outside it 60%" in x for x in t)
    assert "Not a backtest" in b["note"]


def test_narrow_day_range_ratio():
    rows = [H("above", 103, width=0.1, high=120, low=80)] * 10 + [H("above", 103, width=1.0)] * 30
    t = " ".join(i["text"] for i in Wt.cpr_behaviour(rows)["insights"])
    assert "4.00×" in t and "wider-range" in t


def test_too_little_history():
    assert Wt.cpr_behaviour([H("above", 103)] * 5)["insights"] == []


def test_virgin_revisit_rate_by_hand():
    base = [H("above", 103, top=101, bottom=99, high=105, low=95) for _ in range(40)]
    base[10] = H("above", 120, top=101, bottom=99, high=125, low=110, virgin=True)    # untouched
    for j in range(11, 16):
        base[j] = H("above", 130, top=125, bottom=124, high=135, low=120)            # never back to 99-101
    base[20] = H("above", 120, top=101, bottom=99, high=125, low=110, virgin=True)    # revisited next day (default rows)
    t = " ".join(i["text"] for i in Wt.cpr_behaviour(base)["insights"])
    assert "within 5 sessions 50%" in t


def test_edge_distance_and_distributions():
    z = Wt.zones([lvl("a", 1010), lvl("b", 1013)], close=1000, atr=50, tol_atr=0.08)["above"][0]
    assert z["edge_distance"] == 10 and z["distance"] == 11.5 and z["abs_edge_distance_atr"] == 0.2
    rows = [dict(H("above", 103), open=100) for _ in range(12)] + [dict(H("below", 98), open=100) for _ in range(12)] \
        + [dict(H("inside", 100), open=100) for _ in range(12)]
    d = {x["open"]: x for x in Wt.cpr_behaviour(rows)["distributions"]}
    assert d["above"]["median"] == pytest.approx(3.0) and d["above"]["pct_up"] == 100 and d["above"]["n"] == 12
    assert d["below"]["median"] == pytest.approx(-2.0)
