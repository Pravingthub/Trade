import json

import build_dashboard as D


def build(tmp_path):
    out = tmp_path / "desk.html"
    D.build(out, "fixture", news_db=tmp_path / "none.db", journal_db=tmp_path / "j.db")
    html = out.read_text()
    return html, json.loads(html.split("const DATA = ")[1].split(";\n")[0])


def test_demo_data_can_never_produce_a_trade_call(tmp_path):
    _, d = build(tmp_path)
    for i in d["instruments"]:
        assert i["verdict"]["call"] == "NO TRADE"
        assert i["verdict"]["gates"][0] == {"gate": "Live or verified market data", "ok": False, "note": "demo prices"}


def test_verdict_logic():
    inst = {"setups": [{"backtest_status": "candidate_for_paper_trading", "eligible": True}]}
    ok = {"demo": False, "event_risk_today": False, "announcement_risk": False}
    assert D.verdict(inst, ok)["call"] == "WATCH"
    assert D.verdict(inst, {**ok, "event_risk_today": True})["call"] == "STAND ASIDE"
    assert D.verdict({"setups": [{"backtest_status": "rejected", "eligible": False}]}, ok)["call"] == "NO TRADE"


def test_option_check_explains_a_bad_plan(tmp_path):
    _, d = build(tmp_path)
    o = d["instruments"][0]["options"]
    if o and o["plan"]["underlying_rr"] < 2:
        assert o["status"] == "no_trade" and "no strike can fix" in o["reason"]


def test_page_loads_nothing_but_fonts_and_has_empty_states(tmp_path):
    html, d = build(tmp_path)
    assert "<script src" not in html
    assert d["news"] == [] and d["news_health"]["never_fetched"] is True
    assert d["journal"]["closed"] == 0 and d["guard"]["can_recommend"] is True


def test_tone_mapping():
    assert D.tone_of("x", "trending_bullish") == "bull" and D.tone_of("x", "below") == "bear"
    assert D.tone_of("x", "conflicted") == "warn" and D.tone_of("x", "normal") == "neutral"


def test_watch_and_behaviour_present_for_indices(tmp_path):
    _, d = build(tmp_path)
    for i in d["instruments"][:2]:
        w = i["watch"]
        assert w["headline"][0].startswith("Close ")
        assert all(z["low"] > i["brief"]["last_close"] for z in w["above"])
        assert all(z["high"] < i["brief"]["last_close"] for z in w["below"])
        assert [z["mid"] for z in w["above"]] == sorted(z["mid"] for z in w["above"])     # nearest first
        assert i["behaviour"]["insights"] and "Not a backtest" in i["behaviour"]["note"]
