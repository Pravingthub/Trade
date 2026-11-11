import pytest

import pravin_candidates as C
from pravin_quant import RiskPolicy

POL = RiskPolicy(capital=100_000)
OK = dict(strategy_id="S", strategy_version="1.0", instrument="NIFTY50", direction=1, entry_trigger="t",
          entry=100.0, stop=99.0, target=102.5, regime={"overall": "uptrend", "agreement": "4/4 layers agree"},
          provenance={"status": "LIVE_VERIFIED", "health": "HEALTHY"}, policy=POL, lot_size=50,
          validation_status="candidate_for_paper_trading", guard={"can_recommend": True, "reasons": []},
          calendar={"checked": True}, events=[], announcements=[], facts={"width_label": "narrow", "width_percentile": 8,
                                                                          "close_vs_cpr": "above"})


def mk(**kw):
    return C.build(**{**OK, **kw})


def test_all_fields_present_and_no_score():
    c = mk().to_dict()
    for k in ("strategy_id", "strategy_version", "regime", "provenance", "instrument", "direction", "entry_trigger",
              "stop", "targets", "position", "rupee_risk", "reward_risk", "supporting", "contradicting", "news_events",
              "validation_status", "decision", "reasons"):
        assert k in c
    assert not any(k in c for k in ("score", "probability", "confidence"))


def test_paper_trade_only_when_everything_passes():
    c = mk()
    assert c.decision == "PAPER_TRADE" and c.position["lots"] == 20 and c.rupee_risk == 1000
    assert any("uptrend" in s for s in c.supporting) and any("narrow" in s for s in c.supporting)


@pytest.mark.parametrize("change,why", [
    (dict(provenance={"status": "DEMO", "health": "HEALTHY"}), "not live"),
    (dict(provenance={"status": "LIVE_VERIFIED", "health": "DEGRADED"}), "health"),
    (dict(calendar={"checked": False}), "calendar not checked"),
    (dict(events=[{"today": True, "impact": "high", "date": "d", "event": "RBI"}]), "event today"),
    (dict(announcements=[{"impact": "high", "publisher": "RBI", "title": "x"}]), "announcement"),
    (dict(guard={"can_recommend": False, "reasons": ["daily loss stop hit"]}), "risk guard"),
    (dict(target=101.0), "reward/risk"),
    (dict(lot_size=None), "lot size unverified"),
    (dict(stop=70.0, target=170.0), "size"),
])
def test_any_failed_gate_means_no_trade(change, why):
    c = mk(**change)
    assert c.decision == "NO_TRADE" and any(why in r for r in c.reasons)


def test_untested_setup_is_watch_only():
    assert mk(validation_status="not_backtested").decision == "WATCH_ONLY"


def test_counter_trend_is_contradicting_evidence():
    c = mk(direction=-1, entry=100.0, stop=101.0, target=97.5)
    assert any("against the uptrend" in x for x in c.contradicting)
