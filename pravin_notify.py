"""Telegram notifications: loud only when you must act, silent otherwise.

Rules enforced here for every message:
  loud  (phone buzzes): setup triggered, stance change, your open trade, risk guard,
        high-impact news, data feed stale/back, login reminder, morning plan,
        a result that changes tomorrow's lean
  silent: medium news, zone breaks, day/weekly review, shortlist, job failures
  quiet hours 22:00–07:00 IST: nothing is sent (loud or silent)
  duplicates: never sent twice (keys remembered in data/notify_sent.json)
  buttons on setup alerts: "I took it" / "Skipped" → logged in the journal

    python pravin_notify.py test            # sends one test message
    python pravin_notify.py auto            # what the GitHub job runs: plan / results / failures by time of day
    python pravin_notify.py review|weekly|shortlist
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SENT = ROOT / "data" / "notify_sent.json"
ALERTS = ROOT / "data" / "alerts.json"
QUIET_START, QUIET_END = dtime(22, 0), dtime(7, 0)


def ist_now():
    from pravin_quant import IST
    return datetime.now(IST)


@dataclass
class Notifier:
    token: str
    chat_id: str
    sent_path: Path = SENT
    opener: object = None
    sleep: object = time.sleep
    clock: object = ist_now
    log: list = field(default_factory=list)            # what was sent, for tests and the day review

    # ---------------------------------------------------------------- transport
    def call(self, method: str, params: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                       for k, v in params.items()}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        for attempt in range(4):
            try:
                with (self.opener or urllib.request.urlopen)(req, timeout=20) as r:
                    res = json.loads(r.read().decode())
                if res.get("ok"):
                    return res
                raise RuntimeError(res.get("description", "telegram error"))
            except Exception:
                if attempt == 3:
                    raise
                self.sleep(2 ** attempt)
        return {}

    def _sent(self) -> dict:
        try:
            return json.loads(self.sent_path.read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, key: str) -> None:
        s = self._sent()
        s[key] = self.clock().isoformat(timespec="minutes")
        if len(s) > 2000:                                   # keep the file small
            s = dict(sorted(s.items(), key=lambda kv: kv[1])[-1500:])
        self.sent_path.parent.mkdir(parents=True, exist_ok=True)
        self.sent_path.write_text(json.dumps(s))

    # ---------------------------------------------------------------- send
    def notify(self, text: str, *, loud: bool, key: str | None = None, buttons: list | None = None) -> str:
        now = self.clock()
        t = now.time()
        if t >= QUIET_START or t < QUIET_END:
            return "quiet_hours"
        k = hashlib.sha256((key or text).encode()).hexdigest()[:16]
        if k in self._sent():
            return "duplicate"
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        for n, ch in enumerate(chunks):
            p = {"chat_id": self.chat_id, "text": ch, "disable_web_page_preview": "true",
                 "disable_notification": "false" if loud else "true"}
            if buttons and n == len(chunks) - 1:
                p["reply_markup"] = {"inline_keyboard": [buttons]}
            self.call("sendMessage", p)
        self._remember(k)
        self.log.append({"at": now.isoformat(timespec="minutes"), "loud": loud, "text": text[:200]})
        return "sent"

    def send(self, text: str, dedupe_key: str | None = None, loud: bool = True) -> str:
        """Backward-compatible entry point used by older code."""
        return self.notify(text, loud=loud, key=dedupe_key)


def from_env(env: dict | None = None) -> Notifier | None:
    if env is None:
        import os

        from pravin_kite import load_env
        env = {**load_env(), **{k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")}}
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        return Notifier(env["TELEGRAM_BOT_TOKEN"], str(env["TELEGRAM_CHAT_ID"]))
    return None


# ==================================================================== message builders
def f(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}"


def plan_message(data: dict) -> str:
    """The 08:50 morning message: everything needed to start the day."""
    b, m, r = data["brief"], data.get("market_read") or {}, data["risk"]
    L = [("⚠️ DEMO PRICES — practice only\n" if b.get("demo") else "") + f"☀️ Vriddhi · plan for {b['for_session']}"]
    if m:
        L.append(f"Overnight lean: {m['lean'].upper()} ({m['score']:+d}). {m['how_to_use']}")
    for e in b.get("events", []):
        L.append(f"📅 {e['event'].split(' (')[0]} → affects {e['affects_session']}"
                 + (f": {e['outcome']['text']}" if e.get("state") == "read" and e.get("outcome") else ""))
    for inst in data["instruments"]:
        p = inst.get("plan")
        if not p:
            continue
        L += ["", f"■ {p['symbol']}: {p['call']}", f"Stance: {p['stance'].replace('_', ' ')}"]
        if p.get("cross_check"):
            L.append(f"Cross-check: {p['cross_check']['text']}")
        for c in p["cases"]:
            if c["direction"] == "WAIT":
                L.append(f"• Opens inside: {c['trigger']}.")
            else:
                L.append(f"• If {c['if']}: {c['direction']} on {c['trigger']}; stop {c['stop']}, target {c['target']}.")
    L += ["", f"Risk today: {r['max_trades']} trades max · ₹{f(r['per_trade'], 0)} each · stop for the day at ₹{f(r['daily_stop'], 0)}.",
          "No new trades after 14:30. Flat by 15:15."]
    return "\n".join(L)


def setup_message(alert_text: str, paper: bool) -> str:
    return ("🟢 SETUP TRIGGERED" + (" · PAPER TRADE" if paper else "")) + "\n" + alert_text


def setup_buttons(alert_id: str) -> list:
    return [{"text": "✅ I took it", "callback_data": f"took:{alert_id}"[:64]},
            {"text": "⏭ Skipped", "callback_data": f"skip:{alert_id}"[:64]}]


def review_message(rv: dict, title: str) -> str:
    if not rv.get("closed"):
        return f"📊 {title}: no closed trades."
    d = rv["discipline"]
    L = [f"📊 {title}", f"Trades {rv['closed']} · net ₹{f(rv['net'], 0)} · win rate {f(rv['win_rate_pct'], 0)}% · avg {f(rv['avg_r'])}R"]
    flags = [(d["late_stop_exits"], f"late stop exits cost ₹{f(d['late_stop_cost'], 0)}"),
             (d["off_plan_trades"], f"trades outside validated setups (net ₹{f(d['off_plan_net'], 0)})"),
             (d["trades_after_guard_said_stop"], "trades after the risk guard said stop"),
             (d["days_over_trade_limit"], "days over the trade limit")]
    bad = [f"⚠ {n} {t}" for n, t in flags if n]
    L += bad or ["✅ No discipline breaks."]
    return "\n".join(L)


def results_message(event: dict, lean_before: str | None, lean_after: str) -> tuple[str, bool]:
    """(text, loud): loud only when the result changes tomorrow's lean."""
    o = event.get("outcome") or {}
    changed = lean_before is not None and lean_before != lean_after
    text = f"📈 {event['event'].split(' (')[0]} is out: {o.get('text', '')}\nTomorrow's overnight lean: {lean_after.upper()}" \
           + (f" (was {lean_before.upper()})." if changed else ".")
    return text, changed


def unread_result_message(event: dict) -> str:
    return (f"⚠️ {event['event'].split(' (')[0]} is out but could not be read safely "
            f"({event.get('state_reason', 'unknown')}). Check the official release; the lean does not include it.")


def shortlist_message(screen: dict) -> str:
    if not screen or not screen.get("top"):
        return "📋 Tomorrow's stock shortlist: nothing qualified."
    L = [f"📋 Tomorrow's stocks ({screen['tradable']} tradable of {screen['universe']})"]
    for t in screen["top"]:
        lp = t["plan"]["long"]
        L.append(f"• {t['symbol']} · lean {t['lean']} · {'; '.join(t['reasons'][:2])}"
                 f"\n  long above {lp['trigger'].split('above ')[-1].split(' after')[0]}, stop {f(lp['stop'])}, {lp['shares']} sh"
                 + (" · RESULTS: event risk" if t.get("event_risk") else ""))
    return "\n".join(L)


def failures_message(failures: list[str]) -> str:
    return "🛠 Vriddhi job notes: " + "; ".join(failures)


# ==================================================================== buttons → journal
def remember_alert(alert_id: str, payload: dict, path: Path = ALERTS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    d = json.loads(path.read_text()) if path.exists() else {}
    d[alert_id] = payload
    path.write_text(json.dumps(d))


def handle_callbacks(n: Notifier, con, policy, offset: int = 0, path: Path = ALERTS) -> int:
    """Poll button presses once; log 'took' as a live journal trade (plan values,
    fill to be confirmed) and 'skip' as a skipped alert. Returns the next offset."""
    import pravin_journal as J
    res = n.call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]})
    alerts = json.loads(path.read_text()) if path.exists() else {}
    for u in res.get("result", []):
        offset = max(offset, u["update_id"] + 1)
        cq = u.get("callback_query")
        if not cq:
            continue
        action, _, aid = cq.get("data", "").partition(":")
        a = alerts.get(aid)
        reply = "Unknown alert."
        if a and action == "took":
            try:
                tid = J.open_trade(con, mode="live", instrument=a["instrument"], kind=a.get("kind", "option"),
                                   side=a["side"], entry=a["entry"], stop=a["stop"], target=a["target"], qty=a["qty"],
                                   setup=a["setup"], validated=a.get("validated", False), from_alert=True, policy=policy,
                                   notes="logged from Telegram; confirm the fill and exit with pravin_journal.py close")
                reply = f"Logged as trade #{tid}. Place your stop order now."
            except Exception as e:                          # never crash the watcher on a button press
                reply = f"Could not log: {e}"
        elif a and action == "skip":
            con.execute("CREATE TABLE IF NOT EXISTS skipped (alert_id TEXT PRIMARY KEY, at TEXT, setup TEXT)")
            con.execute("INSERT OR IGNORE INTO skipped VALUES (?,?,?)", (aid, ist_now().isoformat(), a["setup"]))
            con.commit()
            reply = "Logged as skipped."
        n.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": reply})
        msg = cq.get("message") or {}
        if msg:
            n.call("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                              "reply_markup": {"inline_keyboard": [[{"text": reply[:60], "callback_data": "done"}]]}})
    return offset


# ==================================================================== GitHub job entry point
def auto(desk_html: Path, n: Notifier | None, now: datetime | None = None, failures: list[str] | None = None,
         state_path: Path | None = None) -> list[str]:
    """Decide what to send from the time of day. Returns what was sent (for logs)."""
    if n is None:
        return ["telegram not configured"]
    now = now or ist_now()
    data = json.loads(desk_html.read_text().split("const DATA = ")[1].split(";\n")[0])
    out, t = [], now.time()
    state_path = state_path or ROOT / "data" / "lean_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    lean = (data.get("market_read") or {}).get("lean", "neutral")
    if dtime(8, 30) <= t <= dtime(9, 30):
        out.append(n.notify(plan_message(data), loud=True, key=f"plan-{data['brief']['for_session']}"))
    for e in data["brief"].get("events", []):
        if e.get("state") == "read":
            text, loud = results_message(e, state.get("lean"), lean)
            out.append(n.notify(text, loud=loud, key=f"result-{e['date']}-{e['event'][:30]}"))
        elif e.get("state") == "released_unread" and t >= dtime(16, 15):
            out.append(n.notify(unread_result_message(e), loud=True, key=f"unread-{e['date']}-{e['event'][:30]}-{now:%H}"))
    if failures:
        out.append(n.notify(failures_message(failures), loud=False, key=f"fail-{now:%Y-%m-%d}-{'|'.join(failures)}"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"lean": lean, "at": now.isoformat(timespec="minutes")}))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["test", "auto", "review", "weekly", "shortlist"])
    ap.add_argument("--desk", default=str(ROOT / "index.html"))
    ap.add_argument("--failures", default="", help="semicolon-separated job notes")
    a = ap.parse_args(argv)
    n = from_env()
    if n is None:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (in .env on your Mac, or as GitHub secrets).")
    if a.cmd == "test":
        print(n.notify("✅ Vriddhi is connected. Loud messages buzz; silent ones arrive quietly.", loud=True,
                       key=f"test-{ist_now():%Y%m%d%H%M}"))
    elif a.cmd == "auto":
        print(auto(Path(a.desk), n, failures=[x for x in a.failures.split(";") if x.strip()]))
    elif a.cmd in ("review", "weekly"):
        from datetime import timedelta

        import pravin_journal as J
        days = 0 if a.cmd == "review" else 6
        rv = J.review(J.connect(), ist_now().date() - timedelta(days=days))
        print(n.notify(review_message(rv, "Today's review" if days == 0 else "This week"), loud=False,
                       key=f"{a.cmd}-{ist_now():%Y-%m-%d}"))
    elif a.cmd == "shortlist":
        import pravin_screen as SC
        print(n.notify(shortlist_message(SC.load()), loud=False, key=f"shortlist-{ist_now():%Y-%m-%d}"))


if __name__ == "__main__":
    main()
