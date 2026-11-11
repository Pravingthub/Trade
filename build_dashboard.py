"""Build the Vriddhi analyst desk (a single static HTML page).

Every number, verdict and signal is computed here by the Python engine and
embedded as JSON; the page only displays it.

    python build_dashboard.py                     # demo prices -> dashboard.html
    python build_dashboard.py --provider kite     # your Zerodha history (keep that page PRIVATE)
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pravin_api import Service, make_provider  # noqa: E402
from pravin_quant import IST, RiskPolicy  # noqa: E402

CAPITAL = 100_000


def tone_of(kind: str, value) -> str:
    """bull / bear / neutral / warn colouring for a descriptive fact."""
    bull = {"trending_bullish", "aligned_up", "mostly_up", "above", "higher_value", "overlapping_higher", "up", "bullish"}
    bear = {"trending_bearish", "aligned_down", "mostly_down", "below", "lower_value", "overlapping_lower", "down", "bearish"}
    warn = {"high_volatility", "conflicted", "insufficient_history"}
    if value in bull:
        return "bull"
    if value in bear:
        return "bear"
    if value in warn:
        return "warn"
    return "neutral"


def words(x: str) -> str:
    return str(x).replace("_", " ")


def posture(inst: dict, ind: dict, mtf: dict) -> list[dict]:
    d, w, m = inst["cpr"]["day"], inst["cpr"]["week"], inst["cpr"]["month"]
    rsi = ind["indicators"]["momentum"]["rsi14"]
    adx = ind["indicators"]["trend"]["adx14"]
    rows = [
        ("Regime", words(inst["regime"]), tone_of("regime", inst["regime"])),
        ("Timeframes", words(mtf["alignment"]), tone_of("mtf", mtf["alignment"])),
        ("Close vs daily CPR", d["close_position"], tone_of("pos", d["close_position"])),
        ("Daily CPR vs yesterday", words(d["relation"]), tone_of("rel", d["relation"])),
        ("Daily CPR width", f"{d['width_label']} · {d['width_percentile']:.0f}th pct" if d["width_percentile"] is not None else d["width_label"],
         "warn" if d["width_label"] == "narrow" else "neutral"),
        ("Close vs weekly CPR", w["close_position"], tone_of("pos", w["close_position"])),
        ("Close vs monthly CPR", m["close_position"], tone_of("pos", m["close_position"])),
        ("RSI 14", f"{rsi:.1f}", "bull" if rsi >= 55 else "bear" if rsi <= 45 else "neutral"),
        ("ADX 14", f"{adx:.1f} · {'trending' if adx >= 25 else 'weak trend' if adx >= 18 else 'no trend'}",
         "neutral"),
    ]
    return [{"label": a, "value": b, "tone": c} for a, b, c in rows]


def verdict(inst: dict, brief: dict) -> dict:
    """The desk's call for the day. Deliberately strict: any failed gate means NO TRADE."""
    gates = [
        ("Live or verified market data", not brief["demo"], "demo prices" if brief["demo"] else "Zerodha data"),
        ("A setup has passed backtesting", any(s["backtest_status"] == "candidate_for_paper_trading" for s in inst["setups"]),
         "none validated yet"),
        ("Validated setup qualifies today", any(s["eligible"] for s in inst["setups"]), "none eligible"),
        ("No high-impact event today", not brief["event_risk_today"], "event today"),
        ("No high-impact official announcement", not brief.get("announcement_risk"), "check announcements"),
    ]
    g = [{"gate": a, "ok": bool(b), "note": "" if b else c} for a, b, c in gates]
    if all(x["ok"] for x in g):
        call, why = "WATCH", "Validated setup in play. Wait for the confirming 5-minute close; size from the risk desk."
    elif g[0]["ok"] and g[1]["ok"]:
        call, why = "STAND ASIDE", "No validated setup qualifies today, or event risk is elevated."
    else:
        call, why = "NO TRADE", "Not enough evidence: " + ", ".join(x["note"] for x in g if not x["ok"]) + "."
    return {"call": call, "why": why, "gates": g}


def option_check(service, iid: str, inst: dict, as_of: date) -> dict | None:
    """Illustrative strike check for a long above the CPR top. Uses a synthetic
    chain priced at a fixed volatility because no live option prices exist yet."""
    import pravin_options as O
    if iid not in ("NIFTY50", "BANKNIFTY") or inst.get("error"):
        return None
    spot = inst["last_close"]
    c = inst["cpr"]["day"]
    entry = max(c["top"], spot)
    target = inst["floor"]["r1"]
    stop = min(c["bottom"], entry - 0.25 * inst["atr14"])       # never tighter than a quarter of the daily ATR
    if target <= entry or stop >= entry:
        return None
    step = 50 if iid == "NIFTY50" else 100
    lot = 65 if iid == "NIFTY50" else 30
    days_ahead = (1 - as_of.weekday()) % 7 or 7               # next Tuesday expiry
    expiry = as_of + timedelta(days=days_ahead)
    now = datetime.combine(as_of, datetime.min.time(), IST).replace(hour=9, minute=20)
    T = O.year_fraction(now, expiry)
    chain = []
    for k in range(-6, 7):
        K = round(entry / step) * step + k * step
        mid = O.bs_price(entry, K, T, 0.065, 0.13, 0, "CE")
        half = max(mid * 0.006, 0.05)
        chain.append({"strike": K, "type": "CE", "bid": round(mid - half, 2), "ask": round(mid + half, 2), "oi": 1_000_000})
    res = O.evaluate_chain(chain, spot=entry, direction=1, target=target, stop=stop, minutes=90, now=now, expiry=expiry,
                           lot_size=lot, policy=RiskPolicy(capital=CAPITAL), chain_time=now)
    plan_rr = (target - entry) / (entry - stop)
    return {"plan": {"entry": entry, "target": target, "stop": stop, "underlying_rr": plan_rr, "rule": "long above CPR top, target R1, stop at the CPR "
                     "bottom or 0.25×ATR below entry, whichever is wider", "expiry": expiry.isoformat(), "lot": lot,
                     "iv_assumed": 0.13, "minutes": 90},
            "status": res["status"],
            "reason": (f"the plan itself offers only 1 : {plan_rr:.2f} on the index, below the 1 : 2 minimum; "
                       "no strike can fix that") if plan_rr < 2 and res["status"] != "ok" else res.get("reason"),
            "warnings": res.get("warnings", []),
            "rows": res["candidates"][:9]}


def build(out: Path, provider: str = "fixture", news_db: Path | None = None, journal_db: Path | None = None):
    import pravin_brief as P
    import pravin_journal as J
    import pravin_news as N
    s = Service(make_provider(provider))
    ids = [i.id for i in s.p.get_instruments()]
    news_db = news_db or ROOT / "news.db"
    brief = P.build_brief(s, ids=ids, capital=CAPITAL, news_db=news_db)
    as_of = date.fromisoformat(brief["for_session"])
    by_id = {i["id"]: i for i in brief["instruments"]}
    instruments = []
    for inst in s.p.get_instruments():
        i = inst.id
        b = by_id[i]
        entry = {"meta": inst.to_dict(), "brief": b}
        if not b.get("error"):
            daily = s._daily_before(i, as_of)
            ind = s.indicators(i, "day", as_of)
            mtf = s.multi_timeframe(i, as_of)
            prev_close = float(daily["close"].iloc[-2])
            import pravin_watch as Wt
            cprs = {p: s.cpr(i, p, as_of) for p in ("day", "week", "month")}
            pivs = {p: s.pivots(i, p, as_of) for p in ("day", "week", "month")}
            z = Wt.zones(Wt.collect_levels(cprs, pivs), b["last_close"], b["atr14"])
            behaviour = Wt.cpr_behaviour(s.cpr_history(i, 250, as_of)["sessions"])
            entry.update({
                "watch": {**z, "headline": Wt.headline(cprs, b["last_close"], z["above"], z["below"])},
                "behaviour": behaviour,
                "change": b["last_close"] - prev_close, "change_pct": (b["last_close"] / prev_close - 1) * 100,
                "cpr": cprs, "pivots": pivs,
                "history": s.cpr_history(i, 40, as_of), "indicators": ind, "session": s.session_levels(i, as_of),
                "mtf": mtf, "posture": posture(b, ind, mtf), "verdict": verdict(b, brief),
                "options": option_check(s, i, b, as_of),
            })
        instruments.append(entry)
    news, news_health = [], {"never_fetched": True}
    if news_db.exists():
        con = N.connect(news_db)
        news = [dict(r) for r in con.execute("SELECT publisher, title, link, impact, bias, reasons, affects, published_utc "
                                             "FROM news ORDER BY COALESCE(published_utc, ingested_utc) DESC LIMIT 12")]
        news_health = N.health(con)
    jpath = journal_db or ROOT / "journal.db"
    jcon = J.connect(jpath) if jpath.exists() else J.connect(":memory:")
    now = datetime.now(IST)
    guard = J.guard_for(jcon, now, RiskPolicy(capital=CAPITAL), "paper").status(now)
    data = {
        "status": {"provider": s.p.name, "data_mode": s.p.data_mode.value, "next_session": s.next_session().isoformat(),
                   "last_session": s.p.sessions[-1].isoformat(), "calendar": s.cal.source,
                   "built_ist": now.isoformat(timespec="minutes")},
        "brief": {k: brief[k] for k in ("for_session", "demo", "completeness", "events", "event_risk_today", "global",
                                         "announcement_risk", "risk", "disclaimer")},
        "news": news, "news_health": news_health,
        "journal": J.review(jcon, as_of - timedelta(days=30)), "guard": guard,
        "instruments": instruments, "health": s.data_health(),
    }
    tpl = (ROOT / "dashboard_template.html").read_text()
    out.write_text(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"), default=str)))
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", default=str(ROOT / "dashboard.html"))
    ap.add_argument("--provider", default="fixture", choices=["fixture", "kite"],
                    help="kite = your local Zerodha history. Keep that page private: do not publish it to GitHub Pages.")
    ap.add_argument("--news-db", type=Path)
    a = ap.parse_args()
    build(Path(a.out), a.provider, a.news_db)
