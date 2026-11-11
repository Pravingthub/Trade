import json
from datetime import datetime

import build_dashboard as D
import pravin_plan as PL
from pravin_quant import IST


def desk(tmp_path):
    out = tmp_path / "d.html"
    D.build(out, "fixture", news_db=tmp_path / "n.db", journal_db=tmp_path / "j.db")
    return json.loads(out.read_text().split("const DATA = ")[1].split(";\n")[0])


def test_plan_has_exact_numbers_and_demo_label(tmp_path):
    d = desk(tmp_path)
    p = d["instruments"][0]["plan"]
    assert p["call"] == "PRACTICE ONLY: DEMO PRICES" and p["money"].startswith("No money")
    longs = [c for c in p["cases"] if c["direction"] == "LONG"]
    assert longs and "5-minute candle closes above" in longs[0]["trigger"] and longs[0]["stop"].startswith("index ")
    assert any(c["direction"] == "WAIT" for c in p["cases"])
    assert "Narrow CPR" in p["context"][0] and "× the usual range" in p["context"][0]
    txt = PL.to_text(p)
    assert "If it opens above" in txt and "R:R" in txt


def test_guard_blocks_the_whole_day(tmp_path):
    d = desk(tmp_path)
    inst = d["instruments"][0]
    p = PL.build(inst, {**d["brief"], "demo": False}, None, {"can_recommend": False, "reasons": ["daily stop"]},
                 d["risk"], datetime(2026, 11, 11, 8, 50, tzinfo=IST))
    assert p["call"] == "DONE FOR THE DAY" and p["stance"] == "DONE_FOR_DAY"


def test_global_shock_means_stand_aside(tmp_path):
    d = desk(tmp_path)
    inst = d["instruments"][0]
    p = PL.build(inst, {**d["brief"], "demo": False}, {"tone": "shock", "shocks": ["S&P 500 -2.40%"]},
                 {"can_recommend": True, "reasons": []}, d["risk"], datetime(2026, 11, 11, 8, 50, tzinfo=IST))
    assert p["call"] == "STAND ASIDE" and "09:45" in p["stance_reason"]


def test_real_money_never_offered(tmp_path):
    d = desk(tmp_path)
    for inst in d["instruments"][:2]:
        assert "Paper" in inst["plan"]["money"] or "No money" in inst["plan"]["money"]
