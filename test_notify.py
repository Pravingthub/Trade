import json
from datetime import datetime

import pravin_notify as NT
from pravin_quant import IST, RiskPolicy


class FakeTG:
    """Records Telegram API calls; can return queued getUpdates results."""
    def __init__(self, updates=None):
        self.calls, self.updates = [], updates or []

    def __call__(self, req, timeout):
        method = req.full_url.rsplit("/", 1)[1]
        params = dict(x.split("=", 1) for x in req.data.decode().split("&"))
        self.calls.append((method, params))
        body = {"ok": True, "result": self.updates if method == "getUpdates" else {}}
        return _Resp(json.dumps(body).encode())


class _Resp:
    def __init__(self, b): self.b = b
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def notifier(tmp_path, when=datetime(2026, 11, 12, 10, 0, tzinfo=IST), updates=None):
    fake = FakeTG(updates)
    return NT.Notifier("T", "123", tmp_path / "sent.json", opener=fake, sleep=lambda s: None, clock=lambda: when), fake


def test_loud_and_silent(tmp_path):
    n, fake = notifier(tmp_path)
    n.notify("buzz", loud=True, key="a")
    n.notify("quiet", loud=False, key="b")
    assert fake.calls[0][1]["disable_notification"] == "false" and fake.calls[1][1]["disable_notification"] == "true"


def test_no_duplicates_and_quiet_hours(tmp_path):
    n, fake = notifier(tmp_path)
    assert n.notify("x", loud=True, key="k") == "sent" and n.notify("x", loud=True, key="k") == "duplicate"
    night, f2 = notifier(tmp_path, datetime(2026, 11, 12, 23, 0, tzinfo=IST))
    assert night.notify("y", loud=True, key="z") == "quiet_hours" and f2.calls == []
    early, _ = notifier(tmp_path, datetime(2026, 11, 12, 6, 30, tzinfo=IST))
    assert early.notify("y", loud=True, key="z2") == "quiet_hours"


def test_buttons_attached_to_setup(tmp_path):
    n, fake = notifier(tmp_path)
    n.notify(NT.setup_message("BUY ...", paper=True), loud=True, key="s", buttons=NT.setup_buttons("abc"))
    kb = json.loads(__import__("urllib.parse").parse.unquote_plus(fake.calls[0][1]["reply_markup"]))
    assert [b["callback_data"] for b in kb["inline_keyboard"][0]] == ["took:abc", "skip:abc"]


def test_took_and_skip_buttons_log_to_journal(tmp_path):
    import pravin_journal as J
    con = J.connect(tmp_path / "j.db")
    NT.remember_alert("a1", {"instrument": "NIFTY", "side": "long", "entry": 100, "stop": 95, "target": 110,
                             "qty": 65, "setup": "CPR-NARROW-OPEN", "validated": False}, tmp_path / "alerts.json")
    NT.remember_alert("a2", {"instrument": "NIFTY", "side": "short", "entry": 100, "stop": 105, "target": 90,
                             "qty": 65, "setup": "CPR-REJECTION"}, tmp_path / "alerts.json")
    ups = [{"update_id": 7, "callback_query": {"id": "q1", "data": "took:a1", "message": {"chat": {"id": 1}, "message_id": 9}}},
           {"update_id": 8, "callback_query": {"id": "q2", "data": "skip:a2", "message": {"chat": {"id": 1}, "message_id": 10}}}]
    n, fake = notifier(tmp_path, updates=ups)
    nxt = NT.handle_callbacks(n, con, RiskPolicy(capital=100_000), 0, tmp_path / "alerts.json")
    assert nxt == 9
    t = con.execute("SELECT * FROM trades").fetchone()
    assert t["mode"] == "live" and t["from_alert"] == 1 and t["setup"] == "CPR-NARROW-OPEN"
    assert con.execute("SELECT COUNT(*) FROM skipped").fetchone()[0] == 1
    answered = [p for m, p in fake.calls if m == "answerCallbackQuery"]
    assert len(answered) == 2 and "Logged+as+trade" in answered[0]["text"]


DATA = {"brief": {"for_session": "2026-11-13", "demo": False, "events": [
            {"event": "India CPI inflation for October 2026 (after the close)", "date": "2026-11-12",
             "affects_session": "2026-11-13", "state": "read",
             "outcome": {"bias": "adverse", "text": "CPI 5.91% vs 5.54% last month: hotter → bad for markets ↓"}}]},
        "market_read": {"lean": "bearish", "score": -4, "how_to_use": "The overnight wind favours shorts."},
        "risk": {"max_trades": 3, "per_trade": 1000, "daily_stop": 2000},
        "instruments": [{"plan": {"symbol": "NIFTY 50", "call": "WATCH", "stance": "NEUTRAL", "cases": [
            {"if": "it opens above 100", "direction": "LONG", "trigger": "close above 100", "stop": "index 95", "target": "index 110"},
            {"if": "it opens inside", "direction": "WAIT", "trigger": "wait for a break"}]}}]}


def desk_file(tmp_path, data=DATA):
    p = tmp_path / "index.html"
    p.write_text("<script>const DATA = " + json.dumps(data) + ";\n</script>")
    return p


def test_plan_message_has_lean_event_cases_and_risk():
    m = NT.plan_message(DATA)
    assert "Overnight lean: BEARISH (-4)" in m and "CPI" in m and "If it opens above 100: LONG" in m and "₹2,000" in m


def test_auto_sends_plan_in_the_morning_only(tmp_path):
    n, fake = notifier(tmp_path, datetime(2026, 11, 13, 8, 52, tzinfo=IST))
    NT.auto(desk_file(tmp_path), n, datetime(2026, 11, 13, 8, 52, tzinfo=IST), state_path=tmp_path / "s.json")
    sent = [p["text"] for m, p in fake.calls if m == "sendMessage"]
    assert any("plan+for" in s for s in sent)
    n2, fake2 = notifier(tmp_path / "x", datetime(2026, 11, 13, 13, 0, tzinfo=IST))
    (tmp_path / "x").mkdir()
    NT.auto(desk_file(tmp_path), n2, datetime(2026, 11, 13, 13, 0, tzinfo=IST), state_path=tmp_path / "s2.json")
    assert not any("plan+for" in p["text"] for m, p in fake2.calls if m == "sendMessage")


def test_result_is_loud_only_when_it_changes_the_lean(tmp_path):
    st = tmp_path / "s.json"
    st.write_text(json.dumps({"lean": "neutral"}))
    n, fake = notifier(tmp_path, datetime(2026, 11, 12, 16, 35, tzinfo=IST))
    NT.auto(desk_file(tmp_path), n, datetime(2026, 11, 12, 16, 35, tzinfo=IST), state_path=st)
    res = [p for m, p in fake.calls if m == "sendMessage" and "is+out" in p["text"]]
    assert res and res[0]["disable_notification"] == "false"
    text, loud = NT.results_message(DATA["brief"]["events"][0], "bearish", "bearish")
    assert not loud


def test_unread_result_warns_and_failures_are_silent(tmp_path):
    d = json.loads(json.dumps(DATA))
    d["brief"]["events"][0].update(state="released_unread", state_reason="release not found")
    n, fake = notifier(tmp_path, datetime(2026, 11, 12, 16, 35, tzinfo=IST))
    NT.auto(desk_file(tmp_path, d), n, datetime(2026, 11, 12, 16, 35, tzinfo=IST), ["FRED fetch failed"], tmp_path / "s.json")
    texts = {p["text"]: p["disable_notification"] for m, p in fake.calls if m == "sendMessage"}
    assert any("could+not+be+read" in t for t in texts)
    assert [v for t, v in texts.items() if "job+notes" in t] == ["true"]


def test_review_and_shortlist_messages():
    rv = {"closed": 3, "net": -1200, "win_rate_pct": 33, "avg_r": -0.4,
          "discipline": {"late_stop_exits": 1, "late_stop_cost": 650, "off_plan_trades": 0, "off_plan_net": 0,
                         "trades_after_guard_said_stop": 1, "days_over_trade_limit": 0}}
    m = NT.review_message(rv, "Today's review")
    assert "late stop exits cost ₹650" in m and "after the risk guard" in m and "No discipline" not in m
    assert NT.review_message({"closed": 0}, "x").endswith("no closed trades.")
    sc = {"tradable": 400, "universe": 2100, "top": [{"symbol": "ABC", "lean": "long", "reasons": ["narrow CPR"],
          "event_risk": True, "plan": {"long": {"trigger": "5-minute close above 510.00 after 09:30", "stop": 507, "shares": 333}}}]}
    s = NT.shortlist_message(sc)
    assert "ABC" in s and "510.00" in s and "event risk" in s


def test_unconfigured_is_harmless(tmp_path):
    assert NT.auto(desk_file(tmp_path), None) == ["telegram not configured"]
    assert NT.from_env({}) is None
