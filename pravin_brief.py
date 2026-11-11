"""Morning brief: everything Vriddhi knows before 09:15, in one page.

Sections: data completeness, events, global markets, and for each index the
daily/weekly/monthly CPR, pivots, regime, timeframe alignment, opening
scenarios, today's qualifying setups with their backtest verdicts, and the
day's risk budget. Optional AI narrative (guarded: it may not introduce any
number that is not in the facts). Optional Telegram delivery.

    python pravin_brief.py                      # demo data, prints markdown
    python pravin_brief.py --provider kite      # your local Zerodha history
    python pravin_brief.py --provider kite --telegram --ai
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pravin_quant import IST, RiskPolicy

ROOT = Path(__file__).resolve().parent
NON_ADVICE = ("Analysis only, not investment advice. Levels are calculated, not predictions. "
              "Setups marked 'not validated' have not passed out-of-sample testing.")


# ====================================================================== inputs
def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [x for x in path.read_text().splitlines() if x.strip() and not x.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def calendar_status(path: Path, as_of: date, max_review_age_days: int = 7) -> dict:
    """Distinguishes 'not checked' from 'checked and nothing scheduled'."""
    if not path.exists():
        return {"status": "not_checked", "checked": False, "detail": "events.csv missing", "unverified_rows": 0}
    reviewed = None
    for line in path.read_text().splitlines():
        if line.lower().startswith("# reviewed:"):
            v = line.split(":", 1)[1].strip()
            try:
                reviewed = date.fromisoformat(v) if v else None
            except ValueError:
                reviewed = None
    rows = read_csv_rows(path)
    unverified = sum(1 for r in rows if (r.get("verified") or "").strip().lower() != "yes")
    if reviewed is None:
        return {"status": "not_checked", "checked": False, "reviewed": None, "unverified_rows": unverified,
                "detail": "no review date in events.csv"}
    age = (as_of - reviewed).days
    if age > max_review_age_days:
        return {"status": "stale", "checked": False, "reviewed": reviewed.isoformat(), "unverified_rows": unverified,
                "detail": f"last reviewed {age} days ago"}
    return {"status": "checked", "checked": True, "reviewed": reviewed.isoformat(), "unverified_rows": unverified,
            "detail": f"reviewed {reviewed.isoformat()}"}


READINGS = [   # (keyword in the event name, how to read the outcome for Indian equities)
    ("cpi", "Direction depends on the number. Higher inflation than expected → bad for markets ↓ (more rate hikes likely); "
            "lower → good ↑; in line → usually little reaction."),
    ("inflation", "Direction depends on the number. Higher than expected → bad ↓; lower → good ↑."),
    ("gdp", "Direction depends on the number. Faster growth than expected → good ↑; slower → bad ↓."),
    ("rbi monetary policy", "Rate cut → good ↑ (banks lead); hike → bad ↓; hold → watch the tone: worried about "
                            "inflation = mildly bad, comfortable = mildly good."),
    ("fomc minutes", "Minutes that sound worried about inflation → mildly bad ↓ for India (stronger dollar, foreign selling); "
                     "relaxed → mildly good ↑."),
    ("fomc", "US rate cut or a softer outlook → good ↑ for India (foreign money flows in); hike or tougher outlook → bad ↓."),
    ("results", "Better than expected → good ↑ for that stock and its index; worse → bad ↓."),
]


def event_reading(name: str) -> str:
    n = name.lower()
    for key, text in READINGS:
        if key in n:
            return text
    return "Direction depends on the outcome versus expectations; wait for the release before taking a side."


def affected_session(d: date, time_ist: str, is_session) -> date:
    """The trading session an event can move: events after 15:30 IST (or on a
    non-trading day) affect the next session."""
    after_close = False
    if time_ist:
        hh, mm = (int(x) for x in time_ist.split(":")[:2])
        after_close = (hh, mm) >= (15, 30)
    s = d + timedelta(days=1) if after_close else d
    while is_session is not None and not is_session(s):
        s += timedelta(days=1)
    return s


def load_events(path: Path, start: date, days: int = 7, is_session=None) -> list[dict]:
    """Verified events only. Rows not marked verified=yes are ignored.
    `today` means the event can move the session on `start`."""
    out = []
    for r in read_csv_rows(path):
        if (r.get("verified") or "").strip().lower() != "yes":
            continue
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        aff = affected_session(d, (r.get("time_ist") or "").strip(), is_session)
        if start <= aff < start + timedelta(days=days) or start <= d < start + timedelta(days=days):
            out.append({**r, "date": d.isoformat(), "affects_session": aff.isoformat(), "today": aff == start,
                        "reading": event_reading(r.get("event", ""))})
    return sorted(out, key=lambda r: (r["date"], r.get("time_ist", "")))


def load_global(path: Path, now: datetime, max_age_h: float = 18) -> list[dict]:
    out = []
    for r in read_csv_rows(path):
        try:
            t = datetime.fromisoformat(r["as_of_ist"]).replace(tzinfo=IST)
        except (KeyError, ValueError):
            continue
        age = (now - t).total_seconds() / 3600
        out.append({**r, "age_hours": round(age, 1), "stale": age > max_age_h})
    return out


# ====================================================================== brief
def _r(x, d=2):
    return None if x is None else round(float(x), d)


def scenarios(close: float, c: dict, piv: dict) -> list[dict]:
    """Descriptive opening scenarios with the levels that matter in each.
    These describe where reference levels sit; they are not forecasts."""
    top, bot, pv = c["top"], c["bottom"], c["pivot"]
    return [
        {"if_open": f"above {top:,.2f} (CPR top)",
         "reference": f"CPR {bot:,.2f}–{top:,.2f} below as the first support zone; R1 {piv['r1']:,.2f}, R2 {piv['r2']:,.2f} above."},
        {"if_open": f"inside the CPR ({bot:,.2f}–{top:,.2f})",
         "reference": f"Balance around the pivot {pv:,.2f}; a 5-minute close outside the CPR is the first directional evidence."},
        {"if_open": f"below {bot:,.2f} (CPR bottom)",
         "reference": f"CPR {bot:,.2f}–{top:,.2f} above as the first resistance zone; S1 {piv['s1']:,.2f}, S2 {piv['s2']:,.2f} below."},
    ]


def setup_status(iid: str, day_cpr: dict, weekly: dict, results_dir: Path) -> list[dict]:
    import pravin_backtest as B
    rep = None
    p = results_dir / f"backtest_{iid}.json"
    if p.exists():
        rep = json.loads(p.read_text())
    verdicts = {r["strategy"]["id"]: r for r in (rep or {}).get("results", [])}
    rank = day_cpr["width_classification"].get("percentile")
    out = []
    for s in B.STRATEGIES:
        cond, note = True, ""
        if "narrow_pct" in s.params:
            cond = rank is not None and rank <= s.params["narrow_pct"]
            note = f"needs CPR width at or below the {s.params['narrow_pct']}th percentile (today: {rank:.0f}th)" \
                if rank is not None else "width history insufficient"
        if s.params.get("need_weekly_confluence"):
            inside = weekly["levels"]["bottom"] <= day_cpr["levels"]["pivot"] <= weekly["levels"]["top"]
            cond = cond and inside
            note += "; daily pivot " + ("inside" if inside else "outside") + " the weekly CPR"
        if s.id == "CPR-INSIDE-BREAK":
            cond, note = None, "qualifies only if the session opens inside the CPR"
        if s.id == "CPR-REJECTION":
            cond, note = None, "qualifies only if the session opens outside the CPR and then tests it"
        v = verdicts.get(s.id)
        status = v["verdict"]["status"] if v else "not_backtested"
        oos = v["out_of_sample"] if v else {}
        out.append({"id": s.id, "name": s.name, "conditions_met_premarket": cond, "condition_note": note.strip("; "),
                    "backtest_status": status,
                    "oos": {k: oos.get(k) for k in ("trades", "win_rate_pct", "profit_factor", "expectancy_r")} if v else None,
                    "eligible": cond is not False and status == "candidate_for_paper_trading"})
    return out


def build_brief(service, as_of: date | None = None, ids: list[str] | None = None, capital: float = 100_000,
                events_path: Path = ROOT / "events.csv", global_path: Path = ROOT / "global_markets.csv",
                results_dir: Path = ROOT / "results", now: datetime | None = None,
                news_db: Path | None = None) -> dict:
    now = now or datetime.now(IST)
    as_of = as_of or service.next_session()
    ids = ids or [i.id for i in service.p.get_instruments() if i.segment == "index"]
    prov = service.p
    completeness = [{"input": "market data", "status": prov.data_mode.value,
                     "detail": f"{prov.name}; last session {prov.sessions[-1]}"}]
    if prov.data_mode.value == "simulated":
        completeness[0]["warning"] = "DEMO: prices are simulated"
    events = load_events(events_path, as_of, is_session=service.cal.is_session)
    calendar = calendar_status(events_path, as_of)
    glob = load_global(global_path, now)
    completeness.append({"input": "event calendar",
                         "status": ("checked: " + (f"{len(events)} event(s) in 7 days" if events else "nothing scheduled in 7 days"))
                         if calendar["checked"] else calendar["status"].replace("_", " "),
                         "detail": calendar["detail"] + (f"; {calendar['unverified_rows']} unverified row(s) ignored"
                                                         if calendar["unverified_rows"] else "")})
    completeness.append({"input": "global markets", "status": "missing" if not glob else
                         ("stale" if any(g["stale"] for g in glob) else "ok"),
                         "detail": f"{len(glob)} rows" if glob else "no licensed feed configured; global_markets.csv empty"})
    news_db = news_db or ROOT / "news.db"
    announcements = []
    if news_db.exists():
        import pravin_news as N
        con = N.connect(news_db)
        h = N.health(con, now)
        announcements = [{k: r[k] for k in ("publisher", "title", "link", "impact", "reasons", "affects", "published_utc")}
                         for r in N.latest(con, hours=18, min_impact="medium", now=now)][:12]
        st = "failing" if h["failing"] else "stale" if h["stale"] else "ok"
        detail = f"official feeds (RBI/SEBI/exchanges); {len(announcements)} notable in 18h"
        if h["failing"]:
            detail += "; failing: " + ", ".join(h["failing"])
        completeness.append({"input": "official announcements", "status": st, "detail": detail})
    else:
        completeness.append({"input": "official announcements", "status": "missing", "detail": "run pravin_news.py fetch"})
    completeness.append({"input": "commercial news / global feeds", "status": "not_configured",
                         "detail": "no licensed source selected yet"})
    has_results = any((results_dir / f"backtest_{i}.json").exists() for i in ids)
    completeness.append({"input": "backtests", "status": "ok" if has_results else "missing",
                         "detail": "" if has_results else "run pravin_backtest.py to validate setups"})
    high_event_today = [e for e in events if e["today"] and e.get("impact") == "high"]

    pol = RiskPolicy(capital=capital)
    instruments = []
    from pravin_quant import QuantError
    for iid in ids:
        try:
            d, w, m = (service.cpr(iid, p, as_of) for p in ("day", "week", "month"))
        except QuantError as e:          # e.g. yesterday's candles not downloaded yet
            instruments.append({"id": iid, "name": service.p.instrument(iid).name, "error": str(e)})
            completeness[0]["status"] = "incomplete"
            completeness[0]["detail"] += f"; {iid}: {e}"
            continue
        piv = service.pivots(iid, "day", as_of)
        ind = service.indicators(iid, "day", as_of)
        mtf = service.multi_timeframe(iid, as_of)
        close = d["reference_price"]["value"]
        instruments.append({
            "id": iid, "name": service.p.instrument(iid).name, "last_close": close,
            "last_close_label": d["reference_price"]["label"],
            "cpr": {p: {"top": _r(x["levels"]["top"]), "bottom": _r(x["levels"]["bottom"]), "pivot": _r(x["levels"]["pivot"]),
                        "width_pct": _r(x["levels"]["width_pct"], 3), "width_label": x["width_classification"]["label"],
                        "width_percentile": _r(x["width_classification"].get("percentile"), 0),
                        "relation": x["relation_to_previous"], "close_position": x["reference_price"]["position"],
                        "source": f"{x['source_ohlc']['start']}..{x['source_ohlc']['end']}"}
                    for p, x in (("day", d), ("week", w), ("month", m))},
            "floor": {k: _r(v) for k, v in piv["floor"].items()},
            "camarilla": {k: _r(v) for k, v in piv["camarilla"].items() if k != "multiplier"},
            "previous_day": {"high": d["source_ohlc"]["high"], "low": d["source_ohlc"]["low"], "close": d["source_ohlc"]["close"]},
            "regime": ind["regime"]["label"], "atr14": _r(ind["indicators"]["volatility"]["atr14"]),
            "rsi14": _r(ind["indicators"]["momentum"]["rsi14"], 1), "adx14": _r(ind["indicators"]["trend"]["adx14"], 1),
            "timeframes": mtf["alignment"],
            "scenarios": scenarios(close, d["levels"], piv["floor"]),
            "setups": setup_status(iid, d, w, results_dir),
        })
    return {
        "for_session": as_of.isoformat(), "generated_ist": now.isoformat(timespec="minutes"), "calendar": calendar,
        "demo": prov.data_mode.value == "simulated",
        "completeness": completeness, "events": events, "event_risk_today": bool(high_event_today),
        "global": glob, "announcements": announcements,
        "announcement_risk": any(a["impact"] == "high" for a in announcements), "instruments": instruments,
        "risk": {"capital": capital, "per_trade": pol.risk_budget,
                 "daily_stop": capital * pol.daily_loss_stop_pct / 100, "max_trades": pol.max_trades_per_day,
                 "min_reward_risk": pol.min_reward_risk},
        "disclaimer": NON_ADVICE,
    }


# ====================================================================== rendering
LABEL = {"higher_value": "higher value", "lower_value": "lower value", "overlapping_higher": "overlapping, higher",
         "overlapping_lower": "overlapping, lower", "inside": "inside previous", "outside": "outside previous",
         "unchanged": "unchanged"}
STATUS = {"candidate_for_paper_trading": "passed backtest (paper-trade first)", "rejected": "failed backtest",
          "insufficient_sample": "too few trades to judge", "not_backtested": "not validated"}


def to_markdown(b: dict) -> str:
    f = lambda v, d=2: "—" if v is None else f"{v:,.{d}f}"
    day = datetime.fromisoformat(b["for_session"]).strftime("%A %d %B %Y")
    L = [f"# Morning brief for {day}", ""]
    if b["demo"]:
        L += ["**DEMO DATA: prices are simulated.**", ""]
    L += ["## Data completeness", ""]
    L += [f"- {c['input']}: {c['status']}{' — ' + c['detail'] if c.get('detail') else ''}" for c in b["completeness"]]
    L += ["", "## Events this week", ""]
    cal = b["calendar"]
    L += [f"- {e['date']} {e.get('time_ist', '')} {e.get('region', '')}: {e['event']} ({e.get('impact', '?')}; "
          f"affects the {e['affects_session']} session)" for e in b["events"]] or [("- Calendar checked: nothing scheduled." if cal["checked"]
                                     else f"- **Calendar NOT checked** ({cal['detail']}): event risk is unknown.")]
    if b["event_risk_today"]:
        L += ["", "**High-impact event today: expect wider ranges; setups are downgraded to watch-only around the event.**"]
    if b.get("announcements"):
        L += ["", "## Official announcements (last 18 hours)", ""] + [
            f"- [{a['impact']}] {a['publisher']}: {a['title']} ({a['reasons']})" for a in b["announcements"]]
        if b.get("announcement_risk"):
            L += ["", "**High-impact official announcement in the last 18 hours: check it before trading.**"]
    if b["global"]:
        L += ["", "## Global markets", ""] + [
            f"- {g['market']}: {g['last']} ({g['change_pct']}%) as of {g['as_of_ist']}{' — STALE' if g['stale'] else ''}"
            for g in b["global"]]
    for i in b["instruments"]:
        if i.get("error"):
            L += ["", f"## {i['name']}", "", f"**Levels not calculated: {i['error']}.** "
                  "Log in to Kite and run `python pravin_kite.py fetch --years 0.05`, then rebuild the brief."]
            continue
        L += ["", f"## {i['name']}", "",
              f"Last close {f(i['last_close'])} ({i['last_close_label']}). Regime: {i['regime'].replace('_', ' ')}; "
              f"timeframes {i['timeframes'].replace('_', ' ')}; ATR {f(i['atr14'])}, RSI {f(i['rsi14'], 1)}, ADX {f(i['adx14'], 1)}.", "",
              "| Period | CPR bottom | Pivot | CPR top | Width | vs previous | Close is |", "|---|---|---|---|---|---|---|"]
        for p in ("day", "week", "month"):
            x = i["cpr"][p]
            pct = f"{x['width_label']} ({f(x['width_percentile'], 0)}th pct)" if x["width_percentile"] is not None else x["width_label"]
            L.append(f"| {p} | {f(x['bottom'])} | {f(x['pivot'])} | {f(x['top'])} | {pct} | {LABEL[x['relation']]} | {x['close_position']} |")
        fl, cm = i["floor"], i["camarilla"]
        L += ["", f"Floor: R3 {f(fl['r3'])} · R2 {f(fl['r2'])} · R1 {f(fl['r1'])} · S1 {f(fl['s1'])} · S2 {f(fl['s2'])} · S3 {f(fl['s3'])}",
              f"Camarilla: R4 {f(cm['r4'])} · R3 {f(cm['r3'])} · S3 {f(cm['s3'])} · S4 {f(cm['s4'])}",
              f"Previous day: high {f(i['previous_day']['high'])}, low {f(i['previous_day']['low'])}", "", "Opening scenarios:"]
        L += [f"- If it opens {s['if_open']}: {s['reference']}" for s in i["scenarios"]]
        L += ["", "Setups:"]
        for s in i["setups"]:
            o = s["oos"]
            stats = f" — OOS {o['trades']} trades, PF {f(o['profit_factor'])}, exp. {f(o['expectancy_r'])}R" if o and o.get("trades") else ""
            cm = s["conditions_met_premarket"]
            flag = "ELIGIBLE TO WATCH" if s["eligible"] else \
                ("depends on the open" if cm is None else "conditions met" if cm else "not today")
            L.append(f"- {s['name']}: {flag}; {STATUS[s['backtest_status']]}{stats}. {s['condition_note']}")
    r = b["risk"]
    L += ["", "## Risk budget", "",
          f"Per trade ₹{r['per_trade']:,.0f} · daily stop ₹{r['daily_stop']:,.0f} · max {r['max_trades']} trades · "
          f"minimum reward/risk 1:{r['min_reward_risk']:g}. NO TRADE is a valid outcome.", "", f"_{b['disclaimer']}_"]
    if b.get("narrative"):
        L[2:2] = ["## Summary", "", b["narrative"]["summary"], ""]
    return "\n".join(L)


def to_telegram(b: dict) -> str:
    """Compact plain-text version for a phone."""
    day = datetime.fromisoformat(b["for_session"]).strftime("%a %d %b")
    L = [f"Vriddhi · brief for {day}" + (" · DEMO DATA" if b["demo"] else "")]
    missing = [c["input"] for c in b["completeness"] if c["status"] in ("missing", "stale", "not_configured")]
    if missing:
        L.append("Missing: " + ", ".join(missing))
    if b["event_risk_today"]:
        L.append("⚠ High-impact event today")
    for a in [x for x in b.get("announcements", []) if x["impact"] == "high"][:3]:
        L.append(f"⚠ {a['publisher']}: {a['title'][:120]}")
    for i in b["instruments"]:
        if i.get("error"):
            L += ["", f"{i['name']}: levels NOT calculated ({i['error']}). Log in to Kite and refresh data."]
            continue
        c = i["cpr"]["day"]
        L += ["", f"{i['name']}: close {i['last_close']:,.2f}",
              f"CPR {c['bottom']:,.2f}–{c['top']:,.2f} ({c['width_label']}, {LABEL[c['relation']]})",
              f"R1 {i['floor']['r1']:,.2f} · S1 {i['floor']['s1']:,.2f} · PDH {i['previous_day']['high']:,.2f} · PDL {i['previous_day']['low']:,.2f}",
              f"Regime {i['regime'].replace('_', ' ')}"]
        el = [s["name"] for s in i["setups"] if s["eligible"]]
        L.append("Eligible setups: " + (", ".join(el) if el else "none validated for today"))
    L += ["", f"Risk/trade ₹{b['risk']['per_trade']:,.0f} · day stop ₹{b['risk']['daily_stop']:,.0f}", "Analysis only."]
    return "\n".join(L)


# ====================================================================== AI narrative (optional, guarded)
class AIProvider(Protocol):
    def complete_json(self, system: str, user: str) -> str: ...


class OpenAIProvider:
    """Uses the official OpenAI SDK. Model name comes from OPENAI_MODEL; nothing
    is hardcoded. Install with `pip install openai`."""

    def __init__(self, env: dict):
        from openai import OpenAI
        if not env.get("OPENAI_API_KEY") or not env.get("OPENAI_MODEL"):
            raise RuntimeError("Set OPENAI_API_KEY and OPENAI_MODEL in .env to enable the AI summary.")
        self.client, self.model = OpenAI(api_key=env["OPENAI_API_KEY"]), env["OPENAI_MODEL"]

    def complete_json(self, system: str, user: str) -> str:
        r = self.client.responses.create(model=self.model, instructions=system, input=user, max_output_tokens=600)
        return r.output_text


AI_SYSTEM = (
    "You write a short pre-market summary for an Indian index trader who uses CPR and pivot points. "
    "Use ONLY the facts in the JSON you receive. Do not add prices, levels, percentages or dates that are not in it. "
    "Do not predict direction or promise outcomes. Mention missing inputs. "
    'Reply with JSON only: {"summary": "<= 120 words", "watch": ["up to 3 short points"], "uncertainties": ["..."]}'
)
NUM = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*")


def _numbers(text: str) -> set[float]:
    out = set()
    for m in NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def guard_narrative(raw: str, facts: dict) -> tuple[dict | None, str]:
    """Accept the AI output only if it is valid JSON with the expected keys and
    every number in it already appears in the facts (within rounding)."""
    try:
        d = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError:
        return None, "not valid JSON"
    if not isinstance(d, dict) or not isinstance(d.get("summary"), str):
        return None, "missing summary"
    allowed = _numbers(json.dumps(facts))
    allowed |= {round(x, 1) for x in allowed} | {round(x) for x in allowed} | set(range(0, 32))
    text = " ".join([d["summary"], *map(str, d.get("watch", [])), *map(str, d.get("uncertainties", []))])
    bad = [n for n in _numbers(text) if n not in allowed and round(n) not in allowed]
    if bad:
        return None, f"introduced numbers not in the facts: {bad[:5]}"
    return {"summary": d["summary"], "watch": d.get("watch", [])[:3], "uncertainties": d.get("uncertainties", [])}, "ok"


def add_narrative(brief: dict, ai: AIProvider) -> dict:
    facts = {k: brief[k] for k in ("for_session", "completeness", "events", "event_risk_today", "global", "instruments", "risk")}
    facts["announcement_headlines"] = [f"{a['publisher']}: {a['title']}" for a in brief.get("announcements", [])]
    raw = ai.complete_json(AI_SYSTEM, json.dumps(facts, default=str))
    narrative, why = guard_narrative(raw, facts)
    brief["narrative"] = narrative
    brief["narrative_status"] = why
    return brief


# ====================================================================== Telegram
@dataclass
class TelegramSender:
    token: str
    chat_id: str
    log_path: Path = ROOT / ".telegram_sent"
    opener: object = None          # injectable for tests
    sleep: object = time.sleep

    def _post(self, text: str) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        op = self.opener or urllib.request.urlopen
        with op(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def send(self, text: str, dedupe_key: str | None = None) -> str:
        key = hashlib.sha256((dedupe_key or text).encode()).hexdigest()[:16]
        sent = set(self.log_path.read_text().split()) if self.log_path.exists() else set()
        if key in sent:
            return "duplicate_skipped"
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        for ch in chunks:
            for attempt in range(4):
                try:
                    r = self._post(ch)
                    if r.get("ok"):
                        break
                    raise RuntimeError(r.get("description", "telegram error"))
                except Exception:
                    if attempt == 3:
                        raise
                    self.sleep(2 ** attempt + 0.25 * attempt)
        with self.log_path.open("a") as fh:
            fh.write(key + "\n")
        return "sent"


def telegram_from_env(env: dict) -> TelegramSender | None:
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        return TelegramSender(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="fixture", choices=["fixture", "kite"])
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args(argv)
    from pravin_api import Service, make_provider
    from pravin_kite import load_env
    env = load_env()
    b = build_brief(Service(make_provider(a.provider)), capital=a.capital)
    if a.ai:
        try:
            add_narrative(b, OpenAIProvider(env))
            print(f"AI summary: {b['narrative_status']}")
        except Exception as e:      # the brief never depends on AI
            print(f"AI summary skipped: {e}")
    md = to_markdown(b)
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / f"brief_{b['for_session']}.md").write_text(md)
    print(md)
    if a.telegram:
        tg = telegram_from_env(env)
        if not tg:
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        print("Telegram:", tg.send(to_telegram(b), dedupe_key=f"brief-{b['for_session']}"))


if __name__ == "__main__":
    main()
