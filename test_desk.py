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
        assert i["verdict"]["gates"][0]["gate"] == "Data live and verified" and not i["verdict"]["gates"][0]["ok"]
        assert i["provenance"]["status"] == "DEMO"
        assert all(c["decision"] == "NO_TRADE" for c in i["candidates"])
        if i["options"]:
            assert i["options"]["status"] == "illustration" and all(r["lots"] == 0 for r in i["options"]["rows"])


PROV = {"status": "LIVE_VERIFIED", "health": "HEALTHY"}
GUARD = {"can_recommend": True, "reasons": []}
BRIEF = {"event_risk_today": False, "announcement_risk": False, "calendar": {"checked": True, "status": "checked"}}


def test_verdict_logic():
    good = {"setups": [{"backtest_status": "candidate_for_paper_trading", "eligible": True}]}
    assert D.verdict(good, BRIEF, PROV, GUARD)["call"] == "WATCH"
    assert D.verdict({"setups": [{"backtest_status": "rejected", "eligible": False}]}, BRIEF, PROV, GUARD)["call"] == "STAND ASIDE"
    for bad_brief, bad_prov, bad_guard in [
        ({**BRIEF, "event_risk_today": True}, PROV, GUARD),
        ({**BRIEF, "calendar": {"checked": False, "status": "not_checked"}}, PROV, GUARD),
        (BRIEF, {**PROV, "status": "DELAYED"}, GUARD),
        (BRIEF, {**PROV, "health": "DEGRADED"}, GUARD),
        (BRIEF, PROV, {"can_recommend": False, "reasons": ["daily loss stop hit"]}),
    ]:
        assert D.verdict(good, bad_brief, bad_prov, bad_guard)["call"] == "NO TRADE"


def test_no_calculation_left_in_the_page(tmp_path):
    html, d = build(tmp_path)
    for banned in ("capital * 0.05", "max_trades - g.trades_opened_today", "breakeven_underlying - p.entry"):
        assert banned not in html
    assert d["risk"]["trades_left"] == 3 and d["risk"]["weekly_stop"] == 5000


def test_option_check_explains_a_bad_plan(tmp_path):
    _, d = build(tmp_path)
    o = d["instruments"][0]["options"]
    if o and o["plan"]["underlying_rr"] < 2:
        assert o["status"] == "illustration" and "no strike can fix" in o["reason"]


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


def test_index_plans_cross_check_each_other(tmp_path):
    _, d = build(tmp_path)
    n = next(i for i in d["instruments"] if i["meta"]["id"] == "NIFTY50")
    b = next(i for i in d["instruments"] if i["meta"]["id"] == "BANKNIFTY")
    assert n["plan"]["cross_check"]["other"] == "NIFTY BANK" and b["plan"]["cross_check"]["other"] == "NIFTY 50"
    if n["brief"]["cpr"]["day"]["close_position"] != b["brief"]["cpr"]["day"]["close_position"]:
        assert "disagree" in n["plan"]["cross_check"]["text"] or "inside" in n["plan"]["cross_check"]["text"]
    assert d["screen"] is None                               # never shown on demo prices
