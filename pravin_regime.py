"""Market regime in four layers, with deterministic, documented rules.

  short-term  5-minute and 15-minute bars
  intraday    1-hour bars
  swing       daily bars (ADX/DMI, ATR percentile, EMAs: regime-v1)
  higher      weekly bars and position versus weekly/monthly CPR

Layer rule (5m/15m/1h/weekly): with at least 50 completed bars,
  up    = close > EMA20 > EMA50 and EMA20 rising over the last 5 bars
  down  = close < EMA20 < EMA50 and EMA20 falling over the last 5 bars
  range = anything else
Overall rule: the swing layer is the anchor and the higher layer is context.
Disagreements are stated in words, never averaged away.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

import pravin_quant as Q

VERSION = "regime-stack-1"


def trend_label(bars: pd.DataFrame) -> dict:
    c = bars["close"]
    if len(c) < 50:
        return {"label": "insufficient", "bars": len(c)}
    e20, e50 = Q.ema(c, 20), Q.ema(c, 50)
    slope = e20.iloc[-1] - e20.iloc[-6]
    last, a, b = float(c.iloc[-1]), float(e20.iloc[-1]), float(e50.iloc[-1])
    if last > a > b and slope > 0:
        lab = "up"
    elif last < a < b and slope < 0:
        lab = "down"
    else:
        lab = "range"
    return {"label": lab, "bars": len(c), "close": last, "ema20": a, "ema50": b, "ema20_slope_5": float(slope),
            "last_bar": str(bars.index[-1])}


SWING_MAP = {"trending_bullish": "up", "trending_bearish": "down", "range_bound": "range", "compressed": "range",
             "high_volatility": "volatile", "transition": "range", "uncertain": "insufficient"}


def combine(a: str, b: str) -> str:
    if a == b:
        return a
    if "insufficient" in (a, b):
        return b if a == "insufficient" else a
    return "mixed"


def stack(service, iid: str, as_of: date) -> dict:
    """Build all four layers for `iid` using only bars completed before `as_of`."""
    def before(df):
        keys = df.index if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None else df.index.tz_convert(Q.IST).date
        return df[np.array([k < as_of for k in keys])]
    p = service.p
    m5, m15, h1 = (before(p.get_historical_candles(iid, iv)) for iv in ("5minute", "15minute", "60minute"))
    daily = service._daily_before(iid, as_of)
    wk = Q.aggregate(daily, "week")
    wk = wk[[Q.week_key(e) < Q.week_key(as_of) for e in wk["end"]]]
    t5, t15, t1h, tw = trend_label(m5), trend_label(m15), trend_label(h1), trend_label(wk)
    sw = Q.regime(daily)
    pos_w = service.cpr(iid, "week", as_of)["reference_price"]["position"]
    pos_m = service.cpr(iid, "month", as_of)["reference_price"]["position"]
    htf_pos = "up" if pos_w == pos_m == "above" else "down" if pos_w == pos_m == "below" else "mixed"
    layers = {
        "short_term": {"label": combine(t5["label"], t15["label"]), "detail": {"5m": t5, "15m": t15}},
        "intraday": {"label": t1h["label"], "detail": {"1h": t1h}},
        "swing": {"label": SWING_MAP[sw["label"]], "detail": {"regime_v1": sw["label"], **sw["evidence"]}},
        "higher": {"label": combine(tw["label"], htf_pos), "detail": {"weekly": tw, "vs_weekly_cpr": pos_w, "vs_monthly_cpr": pos_m}},
    }
    return {"version": VERSION, "layers": layers, **overall(layers)}


def overall(layers: dict) -> dict:
    s, h, i, st = (layers[k]["label"] for k in ("swing", "higher", "intraday", "short_term"))
    notes = []
    if s == "volatile":
        lab = "high_volatility"
        notes.append("Daily volatility is in its top decile: wider stops or smaller size; levels break more easily.")
    elif s in ("up", "down") and h == s:
        lab = "uptrend" if s == "up" else "downtrend"
        notes.append(f"Swing and higher timeframes agree ({s}).")
    elif s in ("up", "down") and h in ("up", "down") and h != s:
        lab = "conflicted"
        notes.append(f"Daily trend is {s} but weekly/monthly structure is {h}: counter-trend swing, lower conviction.")
    elif s in ("up", "down"):
        lab = "uptrend" if s == "up" else "downtrend"
        notes.append(f"Daily trend is {s}; higher timeframes are {h}, so the trend lacks higher-timeframe backing.")
    elif s == "range":
        lab = "range"
        notes.append("Daily trend strength is low: expect rotation between levels"
                     + (f"; higher timeframes lean {h}." if h in ("up", "down") else "."))
    else:
        lab = "uncertain"
        notes.append("Not enough completed daily history to classify the swing layer.")
    trend = {"uptrend": "up", "downtrend": "down"}.get(lab)
    lower = [(n, layers[k]["label"]) for n, k in (("intraday", "intraday"), ("short-term", "short_term"))]
    for name, v in lower:
        if trend and v in ("up", "down") and v != trend:
            notes.append(f"The {name} layer is {v}: a pullback against the {trend}trend, not a reversal until the swing layer changes.")
        elif trend and v == trend:
            notes.append(f"The {name} layer agrees ({v}).")
        elif v in ("range", "mixed"):
            notes.append(f"The {name} layer is {v}.")
    agree = sum(layers[k]["label"] == (trend or "") for k in layers)
    return {"overall": lab, "agreement": f"{agree}/4 layers agree" if trend else "no trend to agree with", "explanation": notes}
