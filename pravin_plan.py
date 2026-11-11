"""Today's plan: one instruction per index, with exact numbers.

Everything the desk knows is reduced to: which setup, what to do on each kind
of open, entry trigger, stop, target, size, when to cancel, and whether this is
paper or real money. Real money is never allowed until the setup has passed the
backtest AND paper trading (no such status exists in the code yet).
"""
from __future__ import annotations

from datetime import datetime

from pravin_stance import StanceEngine

OPTION_RULE = ("Buy the option with delta 0.5–0.7 (about one strike in-the-money) and bid/ask spread under 3%; "
               "the strike check picks the exact contract from the live chain at the trigger.")


def _f(x):
    return f"{x:,.2f}"


def _cand(cands, sid, side):
    return next((c for c in cands if c["strategy_id"] == sid and c["direction"] == side), None)


def _size(c):
    p = c["position"]
    if p.get("lots"):
        return f"{p['lots']} lot(s), ₹{c['rupee_risk']:,.0f} risk"
    return "size: " + p.get("note", "unavailable")


def build(inst: dict, brief: dict, global_summary: dict | None, guard: dict, risk: dict, now: datetime) -> dict:
    b, cands = inst["brief"], inst.get("candidates", [])
    d = b["cpr"]["day"]
    top, bot = inst["cpr"]["day"]["levels"]["top"], inst["cpr"]["day"]["levels"]["bottom"]
    narrow = d["width_label"] == "narrow"
    trend_sid = "CPR-NARROW-OPEN" if narrow else "CPR-INSIDE-BREAK"
    st = {s["id"]: s for s in b["setups"]}

    def case(side, when):
        c = _cand(cands, trend_sid, side) or _cand(cands, "CPR-INSIDE-BREAK", side)
        if not c:
            return None
        word = "above" if side == "long" else "below"
        level = top if side == "long" else bot
        return {"if": when, "setup": c["strategy_id"], "direction": side.upper(),
                "trigger": f"a 5-minute candle closes {word} {_f(level)} after 09:30",
                "entry": f"{'buy' if side == 'long' else 'sell'} at the open of the next 5-minute candle",
                "stop": f"index {_f(c['stop'])}", "target": f"index {_f(c['targets'][0])}",
                "reward_risk": c["reward_risk"], "size": _size(c),
                "option": OPTION_RULE.replace("CE", "CE") if side == "long" else OPTION_RULE.replace("in-the-money", "in-the-money (PE)"),
                "cancel_if": f"no trigger by 14:30, or a 5-minute close back {'below' if side == 'long' else 'above'} "
                             f"{_f(bot if side == 'long' else top)} before entry",
                "exit": "stop, target, or 15:15, whichever comes first", "decision": c["decision"]}

    cases = [x for x in (case("long", f"it opens above {_f(top)}"), case("short", f"it opens below {_f(bot)}")) if x]
    inside = _cand(cands, "CPR-INSIDE-BREAK", "long")
    cases.append({"if": f"it opens inside {_f(bot)}–{_f(top)}", "setup": "CPR-INSIDE-BREAK", "direction": "WAIT",
                  "trigger": "no trade until a 5-minute close outside the CPR after 09:30; then follow the long or short line above",
                  "entry": "—", "stop": "—", "target": "—", "reward_risk": inside["reward_risk"] if inside else None,
                  "size": "—", "option": "—", "cancel_if": "no break by 14:30", "exit": "—",
                  "decision": inside["decision"] if inside else "NO_TRADE"})

    validated = any(st.get(c["setup"], {}).get("backtest_status") == "candidate_for_paper_trading" for c in cases)
    eng = StanceEngine(inst["meta"]["symbol"], top, bot)
    shocks = (global_summary or {}).get("shocks", [])
    ch = eng.premarket(guard_ok=guard["can_recommend"], event_today=brief["event_risk_today"], global_shock=shocks, at=now)
    if not guard["can_recommend"]:
        call = "DONE FOR THE DAY"
    elif brief["demo"]:
        call = "PRACTICE ONLY: DEMO PRICES"
    elif eng.stance == "STAND_ASIDE":
        call = "STAND ASIDE"
    else:
        call = "PAPER-TRADE PLAN" if validated else "WATCH: SETUP NOT YET TESTED"
    money = "No money: demo prices." if brief["demo"] else \
        ("Paper trade only: the setup passed the backtest but not paper trading yet." if validated
         else "Paper trade only: this setup has not been backtested yet.")
    g = global_summary or {}
    beh = inst.get("behaviour", {})
    r = beh.get("narrow_range_ratio")
    if narrow and r is not None:
        verdict = ("they did tend to move more" if r > 1.1 else "little evidence they move more" if r >= 0.95 else "they moved less")
        width_line = (f"Narrow CPR ({d['width_percentile'] or 0:.0f}th percentile). On this index, narrow-CPR days had "
                      f"{r:.2f}× the usual range over {beh.get('narrow_n', 0)} days: {verdict}.")
    else:
        width_line = f"{d['width_label'].capitalize()} CPR ({d['width_percentile'] or 0:.0f}th percentile)."
    ctx = [width_line,
           "Regime: " + inst["regime"]["explanation"][0]]
    w = inst["watch"]
    if w["above"]:
        ctx.append(f"First resistance {w['above'][0]['low']:,.0f}–{w['above'][0]['high']:,.0f}; "
                   f"first support {w['below'][0]['low']:,.0f}–{w['below'][0]['high']:,.0f}." if w["below"] else "")
    ctx.append(f"Global: {g.get('tone', 'not fetched')}" + (f" ({', '.join(shocks)})" if shocks else "")
               + ("" if g else " — run pravin_global.py"))
    for e in brief["events"]:
        ctx.append(f"Event: {e['event']} — moves the {e['affects_session']} session.")
    return {"instrument": inst["meta"]["id"], "symbol": inst["meta"]["symbol"], "call": call, "money": money,
            "stance": eng.stance, "stance_reason": ch.reason if ch else "Waiting for the open.",
            "cases": cases, "context": [c for c in ctx if c],
            "rules": [f"Max {risk['max_trades']} trades; ₹{risk['per_trade']:,.0f} risk each; stop for the day at "
                      f"₹{risk['daily_stop']:,.0f} loss or {risk['loss_lockout']} losses in a row.",
                      "No new trades after 14:30; everything flat by 15:15.",
                      "Log the plan in the journal before entering; place the stop order immediately after."]}


def to_text(p: dict) -> str:
    L = [f"{p['symbol']}: {p['call']}", p["money"], f"Stance: {p['stance'].replace('_', ' ')} — {p['stance_reason']}"]
    for c in p["cases"]:
        if c["direction"] == "WAIT":
            L.append(f"• If {c['if']}: {c['trigger']}.")
        else:
            L.append(f"• If {c['if']}: {c['direction']} when {c['trigger']}; stop {c['stop']}, target {c['target']} "
                     f"(R:R {c['reward_risk']:.2f}); {c['size']}.")
    L += [f"– {x}" for x in p["context"]]
    return "\n".join(L)
