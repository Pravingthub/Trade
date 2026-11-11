import json
from datetime import date, datetime

import pytest

import pravin_brief as P
from pravin_api import Service, make_provider
from pravin_quant import IST

SVC = Service(make_provider("fixture"))


@pytest.fixture
def brief(tmp_path):
    (tmp_path / "events.csv").write_text(
        "date,time_ist,region,event,impact,source\n# comment\n2026-11-11,10:00,IN,RBI policy,high,rbi.org.in\n"
        "2026-11-13,18:00,US,US CPI,high,bls.gov\n2027-01-01,,IN,Too far,low,x\n")
    (tmp_path / "global.csv").write_text("as_of_ist,market,last,change_pct,source\n"
                                         "2026-11-11T02:30,S&P 500,6800.5,0.4,manual\n2026-11-09T02:30,Nikkei,51000,-1.2,manual\n")
    return P.build_brief(SVC, events_path=tmp_path / "events.csv", global_path=tmp_path / "global.csv",
                         results_dir=tmp_path, now=datetime(2026, 11, 11, 7, 30, tzinfo=IST))


def test_brief_numbers_come_from_the_engine(brief):
    n = next(i for i in brief["instruments"] if i["id"] == "NIFTY50")
    api = SVC.cpr("NIFTY50", "day", date(2026, 11, 11))
    assert n["cpr"]["day"]["top"] == round(api["levels"]["top"], 2)
    assert n["floor"]["r1"] == round(SVC.pivots("NIFTY50", "day", date(2026, 11, 11))["floor"]["r1"], 2)


def test_completeness_flags_missing_and_stale_inputs(brief):
    c = {x["input"]: x for x in brief["completeness"]}
    assert c["market data"]["warning"].startswith("DEMO")
    assert c["global markets"]["status"] == "stale"          # Nikkei row is 2 days old
    assert c["news"]["status"] == "not_configured"
    assert c["backtests"]["status"] == "missing"


def test_events_window_and_event_risk(brief):
    assert [e["event"] for e in brief["events"]] == ["RBI policy", "US CPI"]
    assert brief["event_risk_today"] is True


def test_no_setup_is_eligible_without_a_passed_backtest(brief):
    for i in brief["instruments"]:
        assert not any(s["eligible"] for s in i["setups"])
        assert all(s["backtest_status"] == "not_backtested" for s in i["setups"])


def test_setup_eligible_only_when_backtest_passed_and_conditions_met(tmp_path):
    rep = {"results": [{"strategy": {"id": "CPR-NARROW-OPEN"}, "verdict": {"status": "candidate_for_paper_trading"},
                        "out_of_sample": {"trades": 40, "win_rate_pct": 45, "profit_factor": 1.4, "expectancy_r": 0.2}}]}
    (tmp_path / "backtest_NIFTY50.json").write_text(json.dumps(rep))
    b = P.build_brief(SVC, ids=["NIFTY50"], results_dir=tmp_path, events_path=tmp_path / "x", global_path=tmp_path / "y")
    s = {x["id"]: x for x in b["instruments"][0]["setups"]}
    assert s["CPR-NARROW-OPEN"]["eligible"] is (s["CPR-NARROW-OPEN"]["conditions_met_premarket"] is True)
    assert s["CPR-INSIDE-BREAK"]["eligible"] is False         # never backtested


def test_markdown_and_telegram_render(brief):
    md, tg = P.to_markdown(brief), P.to_telegram(brief)
    assert "DEMO DATA" in md and "High-impact event today" in md and "STALE" in md
    assert "NO TRADE is a valid outcome" in md
    assert "DEMO DATA" in tg and len(tg) < 3900


# ---------------------------------------------------------------- AI guard
FACTS = {"close": 25123.45, "cpr_top": 25100.0, "events": ["RBI policy"]}


def test_ai_guard_accepts_grounded_text():
    raw = json.dumps({"summary": "Close 25,123.45 sits above the CPR top 25,100.00. RBI policy is today.",
                      "watch": ["CPR top 25100"], "uncertainties": ["no news feed"]})
    n, why = P.guard_narrative(raw, FACTS)
    assert why == "ok" and n["summary"].startswith("Close")


def test_ai_guard_rejects_invented_numbers():
    raw = json.dumps({"summary": "Expect a move to 25,600 with 70% probability.", "watch": []})
    n, why = P.guard_narrative(raw, FACTS)
    assert n is None and "25600" in why


def test_ai_guard_rejects_bad_json():
    assert P.guard_narrative("Sure! Here's your summary", FACTS)[0] is None


def test_brief_survives_hallucinating_ai(brief):
    class Liar:
        def complete_json(self, system, user):
            assert "Use ONLY the facts" in system
            return json.dumps({"summary": "NIFTY will hit 99,999 today."})
    out = P.add_narrative(brief, Liar())
    assert out["narrative"] is None and "99999" in out["narrative_status"]
    assert "## Summary" not in P.to_markdown(out)


# ---------------------------------------------------------------- Telegram
class FakeResp:
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_telegram_send_dedupe_and_split(tmp_path):
    calls = []

    def opener(req, timeout):
        calls.append(req.data)
        return FakeResp(b'{"ok": true}')
    tg = P.TelegramSender("T", "C", tmp_path / "log", opener=opener, sleep=lambda s: None)
    assert tg.send("x" * 8000, dedupe_key="brief-1") == "sent"
    assert len(calls) == 3                                     # 3900-char chunks
    assert tg.send("x" * 8000, dedupe_key="brief-1") == "duplicate_skipped"
    assert b"api.telegram.org" not in calls[0]                 # token only in URL, never in body


def test_telegram_retries_then_succeeds(tmp_path):
    n = {"k": 0}

    def opener(req, timeout):
        n["k"] += 1
        if n["k"] < 3:
            raise OSError("network")
        return FakeResp(b'{"ok": true}')
    tg = P.TelegramSender("T", "C", tmp_path / "log", opener=opener, sleep=lambda s: None)
    assert tg.send("hello") == "sent" and n["k"] == 3
