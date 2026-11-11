"""What to watch: merges daily, weekly and monthly levels into confluence
zones, and reads how this instrument has behaved around its CPR.

Everything here is descriptive statistics on completed sessions. It is not a
forecast and not a backtest (no entries, exits or costs).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PERIOD_WEIGHT = {"D": 1.0, "W": 2.0, "M": 3.0}
KIND_WEIGHT = {"cpr": 1.5, "pivot": 1.0, "camarilla": 0.75, "extreme": 1.5}


def collect_levels(cpr: dict, pivots: dict) -> list[dict]:
    """Named levels from every timeframe. cpr/pivots are the service responses keyed day/week/month."""
    out = []
    for p, tag in (("day", "D"), ("week", "W"), ("month", "M")):
        L, F, C, src = cpr[p]["levels"], pivots[p]["floor"], pivots[p]["camarilla"], cpr[p]["source_ohlc"]
        out += [
            {"name": f"{tag} CPR top", "price": L["top"], "tf": tag, "kind": "cpr"},
            {"name": f"{tag} pivot", "price": L["pivot"], "tf": tag, "kind": "cpr"},
            {"name": f"{tag} CPR bottom", "price": L["bottom"], "tf": tag, "kind": "cpr"},
            {"name": f"{tag} R1", "price": F["r1"], "tf": tag, "kind": "pivot"},
            {"name": f"{tag} R2", "price": F["r2"], "tf": tag, "kind": "pivot"},
            {"name": f"{tag} S1", "price": F["s1"], "tf": tag, "kind": "pivot"},
            {"name": f"{tag} S2", "price": F["s2"], "tf": tag, "kind": "pivot"},
            {"name": f"{tag} Cam R3", "price": C["r3"], "tf": tag, "kind": "camarilla"},
            {"name": f"{tag} Cam S3", "price": C["s3"], "tf": tag, "kind": "camarilla"},
            {"name": {"D": "Prev day high", "W": "Prev week high", "M": "Prev month high"}[tag], "price": src["high"], "tf": tag, "kind": "extreme"},
            {"name": {"D": "Prev day low", "W": "Prev week low", "M": "Prev month low"}[tag], "price": src["low"], "tf": tag, "kind": "extreme"},
        ]
    return out


def zones(levels: list[dict], close: float, atr: float, tol_atr: float = 0.08, max_dist_atr: float = 2.5) -> dict:
    """Cluster levels lying within tol_atr x ATR of each other. Strength adds up
    timeframe and level-type weights, so a weekly CPR edge sitting on a monthly
    pivot outranks a lone daily Camarilla line."""
    tol = max(tol_atr * atr, 1e-9)
    lv = sorted((x for x in levels if abs(x["price"] - close) <= max_dist_atr * atr), key=lambda x: x["price"])
    clusters: list[list[dict]] = []
    for x in lv:
        if clusters and x["price"] - clusters[-1][0]["price"] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    out = []
    for c in clusters:
        prices = [m["price"] for m in c]
        score = sum(PERIOD_WEIGHT[m["tf"]] * KIND_WEIGHT[m["kind"]] for m in c)
        lo, hi = min(prices), max(prices)
        mid = (lo + hi) / 2
        edge = lo if lo > close else hi if hi < close else close          # nearest edge to the price
        out.append({"low": lo, "high": hi, "mid": mid, "members": [m["name"] for m in c],
                    "timeframes": sorted({m["tf"] for m in c}, key="DWM".index), "score": round(score, 2),
                    "distance": mid - close, "distance_atr": (mid - close) / atr,
                    "edge_distance": edge - close, "edge_distance_atr": (edge - close) / atr,
                    "abs_edge_distance_atr": abs(edge - close) / atr})
    top = max((z["score"] for z in out), default=1)
    for z in out:
        z["strength"] = int(np.clip(round(z["score"] / top * 5), 1, 5))
        z["confluence"] = len(z["timeframes"]) > 1
    above = sorted([z for z in out if z["low"] > close], key=lambda z: z["mid"])[:4]
    below = sorted([z for z in out if z["high"] < close], key=lambda z: -z["mid"])[:4]
    at = [z for z in out if z["low"] <= close <= z["high"]]
    return {"above": above, "below": below, "at_price": at, "tolerance_points": tol}


def headline(cpr: dict, close: float, above: list, below: list) -> list[str]:
    """Plain-language read of where price sits across timeframes."""
    pos = {p: cpr[p]["reference_price"]["position"] for p in ("day", "week", "month")}
    word = {"above": "above", "below": "below", "inside": "inside"}
    lines = [f"Close {close:,.2f} is {word[pos['day']]} the daily CPR, {word[pos['week']]} the weekly "
             f"and {word[pos['month']]} the monthly."]
    if pos["month"] == "above" and pos["day"] == "below":
        lines.append("Short-term weakness inside a higher-timeframe uptrend: dips toward support zones matter more than breakdowns.")
    elif pos["month"] == "below" and pos["day"] == "above":
        lines.append("Short-term strength inside a higher-timeframe downtrend: rallies into resistance zones matter more than breakouts.")
    elif pos["day"] == pos["week"] == pos["month"] == "above":
        lines.append("Above the CPR on all three timeframes: the structure is aligned up.")
    elif pos["day"] == pos["week"] == pos["month"] == "below":
        lines.append("Below the CPR on all three timeframes: the structure is aligned down.")
    else:
        lines.append("Timeframes disagree: expect two-way trade until price leaves the nearest zone.")
    if above:
        z = above[0]
        lines.append(f"First resistance zone {z['low']:,.0f}–{z['high']:,.0f} ({z['distance']:+,.0f} pts): {', '.join(z['members'][:3])}.")
    if below:
        z = below[0]
        lines.append(f"First support zone {z['low']:,.0f}–{z['high']:,.0f} ({z['distance']:+,.0f} pts): {', '.join(z['members'][:3])}.")
    return lines


def cpr_behaviour(history: list[dict], recent: int = 10) -> dict:
    """How this instrument has behaved around its daily CPR (completed sessions).
    `history` rows come from Service.cpr_history."""
    h = pd.DataFrame(history)
    n = len(h)
    if n < 30:
        return {"sessions": n, "insights": [], "note": "not enough history"}
    h["range"] = h["high"] - h["low"]
    narrow_cut = h["width_pct"].quantile(0.20)
    h["narrow"] = h["width_pct"] <= narrow_cut
    closes_above = h["close"] > h["cpr_top"]
    closes_below = h["close"] < h["cpr_bottom"]
    ins = []

    def add(text, n_obs, tone="neutral"):
        ins.append({"text": text, "n": int(n_obs), "tone": tone})

    up = h[h["open_vs_cpr"] == "above"]
    if len(up):
        k = closes_above[up.index].mean() * 100
        add(f"Opened above the CPR on {len(up)} days; closed above it {k:.0f}% of the time.", len(up),
            "bull" if k >= 60 else "warn" if k < 50 else "neutral")
    dn = h[h["open_vs_cpr"] == "below"]
    if len(dn):
        k = closes_below[dn.index].mean() * 100
        add(f"Opened below the CPR on {len(dn)} days; closed below it {k:.0f}% of the time.", len(dn),
            "bear" if k >= 60 else "warn" if k < 50 else "neutral")
    ins_ = h[h["open_vs_cpr"] == "inside"]
    if len(ins_):
        k = (closes_above[ins_.index] | closes_below[ins_.index]).mean() * 100
        add(f"Opened inside the CPR on {len(ins_)} days; closed outside it {k:.0f}% of the time.", len(ins_))
    nr, wr = h[h["narrow"]], h[~h["narrow"]]
    ratio = None
    if len(nr) >= 5 and len(wr) >= 5:
        ratio = nr["range"].median() / wr["range"].median()
        add(f"Narrow-CPR days (bottom 20% width) had a median range {ratio:.2f}× other days"
            + (": they did tend to be wider-range days." if ratio > 1.1 else
               ": no sign of bigger moves on narrow days here." if ratio < 0.95 else "."), len(nr),
            "bull" if ratio > 1.1 else "warn" if ratio < 0.95 else "neutral")
    v = h["virgin"].mean() * 100
    vi = [k for k in range(len(h) - 5) if h["virgin"].iloc[k]]
    if vi:
        hit = sum(any(h["low"].iloc[k + j] <= h["cpr_top"].iloc[k] and h["high"].iloc[k + j] >= h["cpr_bottom"].iloc[k]
                      for j in range(1, 6)) for k in vi)
        add(f"The CPR went untouched (virgin) on {v:.0f}% of days; price came back to that CPR within 5 sessions "
            f"{hit / len(vi) * 100:.0f}% of the time.", len(vi))
    else:
        add(f"The CPR went untouched (virgin) on {v:.0f}% of days.", n)
    last = h.tail(recent)["relation"].dropna()
    ups = last.isin(["higher_value", "overlapping_higher"]).sum()
    dns = last.isin(["lower_value", "overlapping_lower"]).sum()
    trend = "stepping higher" if ups >= dns + 3 else "stepping lower" if dns >= ups + 3 else "going sideways"
    add(f"Value migration: over the last {len(last)} sessions the CPR has been {trend} ({ups} up, {dns} down).", len(last),
        "bull" if trend == "stepping higher" else "bear" if trend == "stepping lower" else "neutral")
    if "open" not in h:
        h["open"] = np.nan
    h["ret_pct"] = (h["close"] / h["open"] - 1) * 100
    h["next_ret_pct"] = h["ret_pct"].shift(-1)
    dist = []
    for cat in ("above", "inside", "below"):
        g = h[h["open_vs_cpr"] == cat]
        if len(g) >= 10:
            r, nx = g["ret_pct"], g["next_ret_pct"].dropna()
            dist.append({"open": cat, "n": len(g), "median": float(r.median()), "p25": float(r.quantile(.25)),
                         "p75": float(r.quantile(.75)), "pct_up": float((r > 0).mean() * 100),
                         "next_day_median": float(nx.median()) if len(nx) else None})
    return {"sessions": n, "narrow_cut_pct": float(narrow_cut), "insights": ins, "distributions": dist,
            "narrow_range_ratio": None if ratio is None else float(ratio), "narrow_n": int(len(nr)),
            "note": f"Descriptive statistics on the last {n} completed sessions. Not a backtest: no entries, exits or costs."}
