"""Daily stock screen: from ALL NSE stocks to the 3–5 worth watching tomorrow.

Step 1 — universe: every NSE equity in Kite's instrument master (series EQ).
Step 2 — tradability: price >= min_price and median 20-day turnover >=
         min_median_turnover_cr crore. Everything else is dropped: a CPR level
         means little in a stock one order can move.
Step 3 — rank by transparent conditions (no probabilities):
         narrow CPR (≤20th percentile of its own history)   +2
         close outside the CPR (a side is already chosen)   +1
         relative strength vs NIFTY over 5 days, top/bottom +1
         daily ATR at least 1.5% (room to reach a target)   +1
         results/announcement today (flagged as event risk) shown, not scored
Step 4 — a plan per stock, sized for your risk budget: options only when the
         stock has options, its lot size is known and one lot fits the budget;
         otherwise the cash trade with an exact share quantity.

Run in the evening (after the close) so the list is ready for the morning:
    python pravin_screen.py
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import pravin_quant as Q

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "screen.json"


def universe_from_instruments(nse_rows: list[dict], nfo_rows: list[dict]) -> list[dict]:
    """NSE equities (EQ series) plus whether each has stock options, and its lot size."""
    lots = {}
    for r in nfo_rows:
        if r.get("instrument_type") in ("CE", "PE") and r.get("segment") == "NFO-OPT":
            lots.setdefault(r["name"], int(r["lot_size"]))
    out = []
    for r in nse_rows:
        sym = r.get("tradingsymbol", "")
        if r.get("instrument_type") != "EQ" or r.get("segment") != "NSE" or "-" in sym:
            continue
        out.append({"symbol": sym, "name": r.get("name") or sym, "token": int(r["instrument_token"]),
                    "has_options": sym in lots, "lot_size": lots.get(sym)})
    return out


def assess(sym: dict, daily: pd.DataFrame, nifty_ret5: float, cfg: dict) -> dict | None:
    """One stock: tradability filter, conditions, and the facts its plan needs."""
    if len(daily) < 60:
        return None
    d = daily.iloc[-1]
    turnover_cr = float((daily["close"] * daily["volume"]).iloc[-20:].median() / 1e7)
    if d.close < cfg["min_price"] or turnover_cr < cfg["min_median_turnover_cr"]:
        return None
    lvl = Q.cpr(d.high, d.low, d.close)                      # tomorrow's CPR from today's candle
    widths = pd.Series([Q.cpr(r.high, r.low, r.close).width_pct for r in daily.iloc[-251:-1].itertuples()])
    rank = Q.percentile_rank(widths, lvl.width_pct)
    atr = float(Q.atr(daily["high"], daily["low"], daily["close"]).iloc[-1])
    ret5 = float(daily["close"].iloc[-1] / daily["close"].iloc[-6] - 1) * 100
    pos = Q.price_position(d.close, lvl)
    return {**sym, "close": float(d.close), "turnover_cr": turnover_cr, "cpr_top": lvl.top, "cpr_bottom": lvl.bottom,
            "cpr_pivot": lvl.pivot, "width_pct": lvl.width_pct, "width_rank": rank, "atr": atr,
            "atr_pct": atr / d.close * 100, "ret5": ret5, "rs5": ret5 - nifty_ret5, "close_vs_cpr": pos,
            "prev_high": float(d.high), "prev_low": float(d.low)}


def ordinal(n: float) -> str:
    n = int(round(n))
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def score(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    rs = np.array([r["rs5"] for r in rows])
    hi, lo = np.percentile(rs, 90), np.percentile(rs, 10)
    for r in rows:
        why, s = [], 0
        if r["width_rank"] is not None and r["width_rank"] <= 20:
            s += 2
            why.append(f"narrow CPR ({ordinal(r['width_rank'])} percentile)")
        if r["close_vs_cpr"] != "inside":
            s += 1
            why.append(f"closed {r['close_vs_cpr']} its CPR")
        if r["rs5"] >= hi:
            s += 1
            why.append(f"strong vs NIFTY ({r['rs5']:+.1f}% in 5 days)")
        elif r["rs5"] <= lo:
            s += 1
            why.append(f"weak vs NIFTY ({r['rs5']:+.1f}% in 5 days)")
        if r["atr_pct"] >= 1.5:
            s += 1
            why.append(f"moves enough (ATR {r['atr_pct']:.1f}%)")
        lean = "long" if r["close_vs_cpr"] == "above" and r["rs5"] > 0 else \
            "short" if r["close_vs_cpr"] == "below" and r["rs5"] < 0 else "either"
        r.update(points=s, reasons=why, lean=lean)
    return sorted(rows, key=lambda r: (-r["points"], -r["turnover_cr"]))


def plan(r: dict, risk_budget: float, rr: float = 2.0) -> dict:
    """Trigger/stop/target for both sides, sized in shares and (when it fits) option lots."""
    buf = max(r["cpr_top"] - r["cpr_bottom"], 0.25 * r["atr"])
    out = {}
    for side, d in (("long", 1), ("short", -1)):
        entry = r["cpr_top"] if d == 1 else r["cpr_bottom"]
        stop = entry - d * buf
        target = entry + d * rr * buf
        shares = math.floor(risk_budget / buf)
        opt = None
        if r["has_options"] and r["lot_size"]:
            lot_risk = 0.6 * buf * r["lot_size"]                 # ≈ delta 0.6 option, before decay and costs
            opt = (f"options fit: 1 lot (≈₹{lot_risk:,.0f} risk at delta 0.6)" if lot_risk <= risk_budget
                   else f"options: one lot risks ≈₹{lot_risk:,.0f}, above your ₹{risk_budget:,.0f}; trade the shares")
        elif r["has_options"]:
            opt = "options exist; lot size unknown"
        out[side] = {"trigger": f"5-minute close {'above' if d == 1 else 'below'} {entry:,.2f} after 09:30",
                     "stop": stop, "target": target, "shares": shares,
                     "share_risk": shares * buf, "options": opt or "no stock options: trade the shares"}
    return out


def run(universe: list[dict], history: dict[str, pd.DataFrame], nifty_daily: pd.DataFrame, cfg: dict,
        risk_budget: float, announcements: list[dict] | None = None) -> dict:
    nifty_ret5 = float(nifty_daily["close"].iloc[-1] / nifty_daily["close"].iloc[-6] - 1) * 100
    rows, dropped = [], 0
    for u in universe:
        h = history.get(u["symbol"])
        a = assess(u, h, nifty_ret5, cfg) if h is not None else None
        if a is None:
            dropped += 1
            continue
        rows.append(a)
    ranked = score(rows)
    news = announcements or []
    top = []
    for r in ranked[: cfg["top_n"]]:
        hits = [n["title"] for n in news if r["name"].lower() in n["title"].lower() or r["symbol"].lower() in n["title"].lower()]
        top.append({**r, "plan": plan(r, risk_budget), "news": hits[:3],
                    "event_risk": any("result" in h.lower() for h in hits)})
    return {"built": datetime.now(Q.IST).isoformat(timespec="minutes"), "universe": len(universe),
            "tradable": len(rows), "dropped": dropped, "filters": {k: v for k, v in cfg.items() if not k.startswith("_")},
            "top": top}


def save(result: dict, path: Path = OUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, default=float))


def load(path: Path = OUT) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def main():
    """Evening run: refresh daily candles for every tradable NSE stock, then screen."""
    from datetime import timedelta

    import pravin_config as PC
    import pravin_kite as K
    cfg = PC.load()
    pol = PC.risk_policy(cfg)
    kite = K.kite_client()
    uni = universe_from_instruments(kite.instruments("NSE"), kite.instruments("NFO"))
    store = K.LocalStore()
    end = datetime.now(Q.IST).date()
    start = end - timedelta(days=int(cfg["screen"]["history_days"] * 1.5))
    hist = {}
    print(f"{len(uni)} NSE stocks; refreshing daily candles (about {len(uni) / 3 / 60:.0f} minutes at Zerodha's rate limit)")
    for k, u in enumerate(uni, 1):
        key = f"EQ_{u['symbol']}"
        old = store.load(key, "day")
        s = start if old.empty else old.index[-1].date()
        try:
            new = K.fetch_history(kite, u["token"], s, end, "day", log=lambda *a: None)
            if not new.empty:
                store.save(key, "day", new)
            hist[u["symbol"]] = store.load(key, "day")
        except Exception as e:
            print(f"  {u['symbol']}: {e}")
        if k % 200 == 0:
            print(f"  {k}/{len(uni)}")
    nifty = store.load("NIFTY50", "day")
    import pravin_news as N
    ann = [dict(r) for r in N.connect().execute("SELECT title FROM news")] if (ROOT / "news.db").exists() else []
    res = run(uni, hist, nifty, cfg["screen"], pol.risk_budget, ann)
    save(res)
    print(f"Tradable: {res['tradable']} of {res['universe']}. Tomorrow's shortlist:")
    for r in res["top"]:
        print(f"  {r['symbol']:<12} {r['points']} pts · lean {r['lean']} · " + "; ".join(r["reasons"]))


if __name__ == "__main__":
    main()
