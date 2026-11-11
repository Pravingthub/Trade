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
from pravin_quant import IST  # noqa: E402


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


def verdict(inst: dict, brief: dict, prov: dict, guard: dict) -> dict:
    """The desk's call. Deliberately strict: any failed gate means NO TRADE."""
    cal = brief["calendar"]
    gates = [
        ("Data live and verified", prov["status"] == "LIVE_VERIFIED", prov["status"].lower().replace("_", " ")),
        ("Market-data health", prov["health"] == "HEALTHY", prov["health"].lower()),
        ("Event calendar checked", cal["checked"], cal["status"].replace("_", " ")),
        ("No high-impact event today", cal["checked"] and not brief["event_risk_today"],
         "unknown" if not cal["checked"] else "event today"),
        ("No high-impact official announcement", not brief.get("announcement_risk"), "check announcements"),
        ("Risk guard clear", guard["can_recommend"], "; ".join(guard["reasons"])),
        ("A setup has passed backtesting", any(s["backtest_status"] == "candidate_for_paper_trading" for s in inst["setups"]),
         "none validated yet"),
        ("Validated setup qualifies today", any(s["eligible"] for s in inst["setups"]), "none eligible"),
    ]
    g = [{"gate": a, "ok": bool(b), "note": "" if b else c} for a, b, c in gates]
    if all(x["ok"] for x in g):
        call, why = "WATCH", "Validated setup in play. Wait for the confirming 5-minute close; size from the risk desk."
    elif all(x["ok"] for x in g[:6]):
        call, why = "STAND ASIDE", "Data and risk are fine, but no validated setup qualifies today."
    else:
        call, why = "NO TRADE", "Not enough evidence: " + ", ".join(f"{x['gate'].lower()} ({x['note']})" for x in g if not x["ok"]) + "."
    return {"call": call, "why": why, "gates": g}


def option_check(service, iid: str, inst: dict, as_of: date, cfg: dict, policy, store_meta: dict | None = None) -> dict | None:
    """Illustrative strike check for a long above the CPR top. The chain is
    SYNTHETIC (priced by the model at an assumed IV) until a live chain exists,
    so the result can only ever be an illustration."""
    import pravin_config as PC
    import pravin_options as O
    con = PC.contract(iid, cfg, store_meta)
    if con is None or inst.get("error"):
        return None
    spot = inst["last_close"]
    c = inst["cpr"]["day"]
    entry = max(c["top"], spot)
    target = inst["floor"]["r1"]
    stop = min(c["bottom"], entry - 0.25 * inst["atr14"])       # never tighter than a quarter of the daily ATR
    if target <= entry or stop >= entry:
        return None
    expiry = PC.next_expiry(con.expiry_rule, as_of, service.cal.is_session)
    now = datetime.combine(as_of, datetime.min.time(), IST).replace(hour=9, minute=20)
    T = O.year_fraction(now, expiry)
    iv, r = 0.13, cfg["options_model"]["risk_free_rate"]
    chain = []
    for k in range(-6, 7):
        K = round(entry / con.strike_step) * con.strike_step + k * con.strike_step
        mid = O.bs_price(entry, K, T, r, iv, 0, "CE")
        half = max(mid * 0.006, 0.05)
        chain.append({"strike": K, "type": "CE", "bid": round(mid - half, 2), "ask": round(mid + half, 2), "oi": 1_000_000})
    res = O.evaluate_chain(chain, spot=entry, direction=1, target=target, stop=stop, minutes=90, now=now, expiry=expiry,
                           lot_size=con.lot_size, policy=policy, chain_time=now, r=r, source="synthetic",
                           iv_shock=cfg["options_model"]["iv_shock_points"] / 100, strike_step=con.strike_step,
                           is_session=service.cal.is_session)
    plan_rr = (target - entry) / (entry - stop)
    for row in res["candidates"]:
        row["be_points"] = None if row["breakeven_underlying"] is None else row["breakeven_underlying"] - entry
    return {"plan": {"entry": entry, "target": target, "stop": stop, "underlying_rr": plan_rr, "expiry": expiry.isoformat(),
                     "expiry_rule": con.expiry_rule, "lot": con.lot_size, "lot_verified": con.lot_verified,
                     "iv_assumed": iv, "minutes": 90,
                     "rule": "long above CPR top, target R1, stop at the CPR bottom or 0.25×ATR below entry, whichever is wider"},
            "source": "SYNTHETIC", "model": res.get("model"), "status": res["status"],
            "reason": (f"the plan itself offers only 1 : {plan_rr:.2f} on the index, below the 1 : {policy.min_reward_risk:g} "
                       "minimum; no strike can fix that") if plan_rr < policy.min_reward_risk else res.get("reason"),
            "warnings": res.get("warnings", []), "rows": res["candidates"][:9]}


def build(out: Path, provider: str = "fixture", news_db: Path | None = None, journal_db: Path | None = None,
          events_path: Path | None = None):
    import pravin_backtest as BT
    import pravin_brief as P
    import pravin_candidates as CD
    import pravin_config as PC
    import pravin_journal as J
    import pravin_news as N
    import pravin_regime as RG
    from pravin_api import market_health, provenance_status
    cfg = PC.load()
    policy = PC.risk_policy(cfg)
    s = Service(make_provider(provider))
    ids = [i.id for i in s.p.get_instruments()]
    news_db = news_db or ROOT / "news.db"
    brief = P.build_brief(s, ids=ids, capital=policy.capital, news_db=news_db,
                          **({"events_path": events_path} if events_path else {}))
    health = {r["instrument"]: r for r in s.data_health()["instruments"]}
    jpath = journal_db or ROOT / "journal.db"
    jcon = J.connect(jpath) if jpath.exists() else J.connect(":memory:")
    now = datetime.now(IST)
    guard = J.guard_for(jcon, now, policy, "paper").status(now)
    store_meta = getattr(getattr(s.p, "store", None), "meta", lambda: {})()
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
            hrow = health[i]
            stale = hrow["freshness"]["stale"]
            mh = market_health(hrow["issues"], hrow["missing_sessions"], stale, s.p.data_mode.value == "live")
            prov = {"status": provenance_status(s.p.data_mode, stale), "health": mh["status"], "health_reasons": mh["reasons"],
                    "source": s.p.name, "as_of": cprs["day"]["source_ohlc"]["end"], "built_ist": now.isoformat(timespec="minutes")}
            regime_stack = RG.stack(s, i, as_of)
            con_ = PC.contract(i, cfg, store_meta)
            facts = {"width_label": b["cpr"]["day"]["width_label"], "width_percentile": b["cpr"]["day"]["width_percentile"] or 0,
                     "close_vs_cpr": b["cpr"]["day"]["close_position"], "atr14": b["atr14"]}
            cands = CD.premarket(instrument=i, strategies=BT.STRATEGIES, setups_status={x["id"]: x for x in b["setups"]},
                                 cpr_levels=cprs["day"]["levels"], regime=regime_stack, provenance=prov, policy=policy,
                                 lot_size=con_.lot_size if con_ else None, guard=guard, calendar=brief["calendar"],
                                 events=brief["events"], announcements=brief.get("announcements", []), facts=facts)
            entry.update({
                "provenance": prov, "regime": regime_stack, "candidates": [c.to_dict() for c in cands],
                "cpr_levels_day": cprs["day"]["levels"],
                "watch": {**z, "headline": Wt.headline(cprs, b["last_close"], z["above"], z["below"])},
                "behaviour": behaviour,
                "change": b["last_close"] - prev_close, "change_pct": (b["last_close"] / prev_close - 1) * 100,
                "cpr": cprs, "pivots": pivs,
                "history": s.cpr_history(i, 40, as_of), "indicators": ind, "session": s.session_levels(i, as_of),
                "mtf": mtf, "posture": posture(b, ind, mtf), "verdict": verdict(b, brief, prov, guard),
                "options": option_check(s, i, b, as_of, cfg, policy, store_meta),
            })
        if entry.get("verdict") and i in ("NIFTY50", "BANKNIFTY"):
            import pravin_global as GL
            import pravin_plan as PL
            entry["plan"] = PL.build({**entry, "cpr": entry["cpr"]}, brief, GL.load(), guard,
                                     PC.policy_summary(policy, guard["trades_opened_today"]), now)
        instruments.append(entry)
    # NIFTY <-> BANKNIFTY cross-check: each index plan states what the other says.
    by = {e["meta"]["id"]: e for e in instruments}
    for a, b2 in (("NIFTY50", "BANKNIFTY"), ("BANKNIFTY", "NIFTY50")):
        if by.get(a, {}).get("plan") and by.get(b2, {}).get("brief") and not by[b2]["brief"].get("error"):
            pos = by[b2]["brief"]["cpr"]["day"]["close_position"]
            other = by[b2]["meta"]["symbol"]
            txt = {"above": f"{other} closed above its CPR: confirms longs here, argues against shorts.",
                   "below": f"{other} closed below its CPR: confirms shorts here, argues against longs.",
                   "inside": f"{other} closed inside its CPR: no confirmation either way."}[pos]
            mine = by[a]["brief"]["cpr"]["day"]["close_position"]
            agree = pos == mine and pos != "inside"
            by[a]["plan"]["cross_check"] = {"other": other, "other_position": pos, "agrees": agree,
                                            "text": txt + ("" if agree or "inside" in (pos, mine) else
                                                           " The two indices disagree today: a move carried by one sector is less reliable.")}
    news, news_health = [], {"never_fetched": True}
    if news_db.exists():
        con = N.connect(news_db)
        news = [dict(r) for r in con.execute("SELECT publisher, title, link, impact, bias, reasons, affects, published_utc "
                                             "FROM news ORDER BY COALESCE(published_utc, ingested_utc) DESC LIMIT 40")]
        news_health = N.health(con)
    last_fetch = None
    if news_db.exists():
        row = N.connect(news_db).execute("SELECT MAX(at_utc) FROM fetches WHERE ok=1").fetchone()
        last_fetch = row[0] if row else None
    import pravin_marketread as MRD
    market_read = MRD.read(__import__("pravin_global").load(), news, brief["events"], now)
    for e in instruments:
        if e.get("plan"):
            e["plan"]["context"].insert(0, f"Overnight lean: {market_read['lean']} ({market_read['score']:+d}). "
                                           + market_read["how_to_use"])
    data = {
        "status": {"provider": s.p.name, "data_mode": s.p.data_mode.value, "next_session": s.next_session().isoformat(),
                   "last_session": s.p.sessions[-1].isoformat(), "calendar": s.cal.source,
                   "built_ist": now.isoformat(timespec="minutes")},
        "brief": {k: brief[k] for k in ("for_session", "demo", "completeness", "events", "event_risk_today", "global",
                                         "announcement_risk", "risk", "disclaimer", "calendar")},
        "risk": PC.policy_summary(policy, guard["trades_opened_today"]),
        "global": __import__("pravin_global").load(), "market_read": market_read,
        "screen": __import__("pravin_screen").load() if s.p.data_mode.value != "simulated" else None,
        "news": news, "news_health": {**news_health, "last_fetch_utc": last_fetch},
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
    ap.add_argument("--telegram-plan", action="store_true", help="also send today's plan to Telegram")
    a = ap.parse_args()
    build(Path(a.out), a.provider, a.news_db)
    if a.telegram_plan:
        import pravin_plan as PL
        from pravin_brief import telegram_from_env
        from pravin_kite import load_env
        tg = telegram_from_env(load_env())
        data = json.loads(Path(a.out).read_text().split("const DATA = ")[1].split(";\n")[0])
        text = "\n\n".join(PL.to_text(i["plan"]) for i in data["instruments"] if i.get("plan"))
        print(text)
        if tg:
            print("Telegram:", tg.send("Vriddhi · Today's plan\n\n" + text, dedupe_key=f"plan-{data['brief']['for_session']}"))
