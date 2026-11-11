"""Trade candidates: one auditable record per possible trade.

Every candidate carries strategy id/version, regime, data provenance and
freshness, instrument, direction, entry trigger, stop, targets, size, rupee
risk, reward/risk, supporting and contradicting evidence, relevant news and
events, validation status, and the decision with its reasons.

There is deliberately NO score or probability: nothing here is calibrated
out-of-sample yet, so no number may pretend to be a win rate.

Decisions (strictest first):
  NO_TRADE      a hard gate failed (data, calendar, guard, size, reward/risk)
  WATCH_ONLY    gates pass but the setup has not passed backtesting
  PAPER_TRADE   backtest passed; paper-trade before any real money
  (There is no live-trading decision until paper results are reviewed.)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from pravin_quant import RiskPolicy, position_size, reward_risk

ACTIONABLE_DATA = {"LIVE_VERIFIED"}


@dataclass
class TradeCandidate:
    strategy_id: str
    strategy_version: str
    instrument: str
    direction: str
    regime: dict
    provenance: dict
    entry_trigger: str
    entry_level: float
    stop: float
    targets: list
    position: dict
    rupee_risk: float | None
    reward_risk: float
    supporting: list = field(default_factory=list)
    contradicting: list = field(default_factory=list)
    news_events: list = field(default_factory=list)
    validation_status: str = "not_backtested"
    decision: str = "NO_TRADE"
    reasons: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def build(*, strategy_id: str, strategy_version: str, instrument: str, direction: int, entry_trigger: str,
          entry: float, stop: float, target: float, regime: dict, provenance: dict, policy: RiskPolicy,
          lot_size: int | None, validation_status: str, guard: dict, calendar: dict, events: list,
          announcements: list, facts: dict) -> TradeCandidate:
    side = "long" if direction == 1 else "short"
    rr = reward_risk(entry, stop, target, side)
    hard, support, contra = [], [], []
    # --- hard gates -------------------------------------------------------
    if provenance.get("status") not in ACTIONABLE_DATA:
        hard.append(f"data is {provenance.get('status')}, not live and verified")
    if provenance.get("health") not in (None, "HEALTHY"):
        hard.append(f"market-data health is {provenance.get('health')}")
    if not calendar.get("checked"):
        hard.append("event calendar not checked: event risk unknown")
    if any(e.get("today") and e.get("impact") == "high" for e in events):
        hard.append("high-impact event today")
    if any(a.get("impact") == "high" for a in announcements):
        hard.append("high-impact official announcement in the last 18 hours")
    if not guard.get("can_recommend", False):
        hard.append("risk guard: " + "; ".join(guard.get("reasons", [])))
    if rr < policy.min_reward_risk:
        hard.append(f"reward/risk {rr:.2f} below 1:{policy.min_reward_risk:g}")
    if lot_size:
        sz = position_size(policy, entry, stop, lot_size, side)
        position = {"lots": sz.lots, "quantity": sz.quantity, "lot_size": lot_size, "note": sz.reason}
        risk = sz.rupee_risk if sz.ok else None
        if not sz.ok:
            hard.append(f"size: {sz.reason}")
    else:
        position, risk = {"lots": 0, "quantity": 0, "lot_size": None, "note": "lot size unverified"}, None
        hard.append("lot size unverified: cannot size")
    # --- evidence (informative, never a score) --------------------------------
    trend = {"uptrend": 1, "downtrend": -1}.get(regime.get("overall"))
    if trend == direction:
        support.append(f"regime {regime['overall']} ({regime.get('agreement', '')})")
    elif trend == -direction:
        contra.append(f"trade is against the {regime['overall']}")
    elif regime.get("overall") == "conflicted":
        contra.append("timeframes conflict")
    if facts.get("width_label") == "narrow":
        support.append(f"narrow CPR ({facts.get('width_percentile', 0):.0f}th percentile)")
    elif facts.get("width_label") == "wide":
        contra.append("wide CPR: range day more likely")
    pos = facts.get("close_vs_cpr")
    if pos and ((pos == "above") == (direction == 1)) and pos != "inside":
        support.append(f"last close {pos} the daily CPR")
    elif pos and pos != "inside":
        contra.append(f"last close {pos} the daily CPR")
    news = [f"{e['date']} {e['event']} ({e['impact']})" for e in events] + \
           [f"{a['publisher']}: {a['title']} ({a['impact']})" for a in announcements if a.get("impact") != "low"]
    # --- decision --------------------------------------------------------------
    if hard:
        decision, reasons = "NO_TRADE", hard
    elif validation_status != "candidate_for_paper_trading":
        decision, reasons = "WATCH_ONLY", [f"setup is {validation_status.replace('_', ' ')}: watch, do not trade"]
    else:
        decision, reasons = "PAPER_TRADE", ["all gates passed; backtest passed; paper-trade before real money"]
    return TradeCandidate(strategy_id, strategy_version, instrument, side, regime, provenance, entry_trigger, entry, stop,
                          [target], position, risk, rr, support, contra, news, validation_status, decision, reasons)


def premarket(*, instrument: str, strategies, setups_status: dict, cpr_levels: dict, regime: dict, provenance: dict,
              policy: RiskPolicy, lot_size: int | None, guard: dict, calendar: dict, events: list,
              announcements: list, facts: dict) -> list[TradeCandidate]:
    """Planned candidates for setups whose pre-market conditions are met.
    Triggers are conditional on the open; nothing here fires by itself."""
    out = []
    top, bot = cpr_levels["top"], cpr_levels["bottom"]
    for s in strategies:
        st = setups_status.get(s.id, {})
        if st.get("conditions_met_premarket") is False:
            continue
        rr = s.params.get("rr", 2.0)
        for d in (1, -1):
            entry, stop = (top, bot) if d == 1 else (bot, top)
            buffer = max(abs(top - bot), facts.get("atr14", 0) * 0.25)       # never a stop tighter than 0.25 ATR
            stop = entry - d * buffer
            target = entry + d * rr * abs(entry - stop)
            trig = (f"if it opens {'above' if d == 1 else 'below'} the CPR: 5-minute close "
                    f"{'above' if d == 1 else 'below'} {entry:,.2f} after 09:30")
            out.append(build(strategy_id=s.id, strategy_version=s.version, instrument=instrument, direction=d,
                             entry_trigger=trig, entry=entry, stop=stop, target=target, regime=regime,
                             provenance=provenance, policy=policy, lot_size=lot_size,
                             validation_status=st.get("backtest_status", "not_backtested"), guard=guard,
                             calendar=calendar, events=events, announcements=announcements, facts=facts))
    return out
