"""Intraday stance: what Vriddhi wants you to do RIGHT NOW, re-decided on rules.

Stances
  LONG_BIAS     look only for long triggers
  SHORT_BIAS    look only for short triggers
  NEUTRAL       wait (no direction yet, or the previous one was invalidated)
  STAND_ASIDE   temporary: risk event, shock or high-impact news; no new trades
  DONE_FOR_DAY  risk guard tripped or the day's window closed; final

Rules (each change carries one line of reasoning):
  open above CPR → LONG_BIAS; below → SHORT_BIAS; inside → NEUTRAL
  NEUTRAL + 5-minute close outside the CPR after 09:30 → bias in that direction
  bias + 5-minute close back through the far side of the CPR → NEUTRAL (thesis failed)
  high-impact news or a market shock → STAND_ASIDE for 30 minutes, then NEUTRAL
  news whose direction opposes the bias → NEUTRAL
  risk guard tripped → DONE_FOR_DAY; 14:30 → no new trades (DONE_FOR_DAY)

Anti-whipsaw: at most N discretionary changes a day and at least M minutes
apart. Protective changes (STAND_ASIDE, DONE_FOR_DAY) are never blocked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

PROTECTIVE = {"STAND_ASIDE", "DONE_FOR_DAY"}
FIRST_SIGNAL, LAST_ENTRY = time(9, 30), time(14, 30)


@dataclass
class Change:
    at: str
    old: str
    new: str
    reason: str

    def text(self, name: str) -> str:
        return f"{name}: stance {self.old.replace('_', ' ').lower()} → {self.new.replace('_', ' ')}. {self.reason}"


@dataclass
class StanceEngine:
    name: str
    cpr_top: float
    cpr_bottom: float
    max_changes: int = 4
    min_gap: timedelta = timedelta(minutes=30)
    stance: str = "NEUTRAL"
    history: list = field(default_factory=list)
    stand_aside_until: datetime | None = None
    _last_discretionary: datetime | None = None

    def _set(self, new: str, reason: str, at: datetime) -> Change | None:
        if new == self.stance or self.stance == "DONE_FOR_DAY":
            return None
        if new not in PROTECTIVE:
            if self.stand_aside_until and at < self.stand_aside_until:
                return None
            n = sum(1 for c in self.history if c.new not in PROTECTIVE)
            if n >= self.max_changes:
                return None
            if self._last_discretionary and at - self._last_discretionary < self.min_gap and self.history:
                return None
            self._last_discretionary = at
        c = Change(at.isoformat(timespec="minutes"), self.stance, new, reason)
        self.stance = new
        self.history.append(c)
        return c

    # ---------------------------------------------------------------- inputs
    def premarket(self, *, guard_ok: bool, event_today: bool, global_shock: list, at: datetime) -> Change | None:
        if not guard_ok:
            return self._set("DONE_FOR_DAY", "Risk guard says no new trades today.", at)
        if event_today:
            return self._set("STAND_ASIDE", "High-impact event affects today's session.", at)
        if global_shock:
            self.stand_aside_until = at.replace(hour=9, minute=45)
            return self._set("STAND_ASIDE", "Overnight global shock: " + ", ".join(global_shock)
                             + ". Let the open settle until 09:45.", at)
        return None

    def on_open(self, price: float, at: datetime) -> Change | None:
        if price > self.cpr_top:
            return self._set("LONG_BIAS", f"Opened {price:,.2f}, above the CPR top {self.cpr_top:,.2f}.", at)
        if price < self.cpr_bottom:
            return self._set("SHORT_BIAS", f"Opened {price:,.2f}, below the CPR bottom {self.cpr_bottom:,.2f}.", at)
        return self._set("NEUTRAL", "Opened inside the CPR: wait for a 5-minute close outside it after 09:30.", at)

    def on_bar(self, close: float, at: datetime) -> Change | None:
        t = at.time()
        if self.stance == "STAND_ASIDE" and self.stand_aside_until and at >= self.stand_aside_until:
            self.stand_aside_until = None
            self.stance = "NEUTRAL"                                   # re-evaluate below
        if t >= LAST_ENTRY:
            return self._set("DONE_FOR_DAY", "14:30: no new trades today; manage or exit open positions.", at)
        if self.stance == "NEUTRAL" and t >= FIRST_SIGNAL:
            if close > self.cpr_top:
                return self._set("LONG_BIAS", f"5-minute close {close:,.2f} above the CPR top.", at)
            if close < self.cpr_bottom:
                return self._set("SHORT_BIAS", f"5-minute close {close:,.2f} below the CPR bottom.", at)
        if self.stance == "LONG_BIAS" and close < self.cpr_bottom:
            return self._set("NEUTRAL", f"Long thesis failed: 5-minute close {close:,.2f} back below the CPR.", at)
        if self.stance == "SHORT_BIAS" and close > self.cpr_top:
            return self._set("NEUTRAL", f"Short thesis failed: 5-minute close {close:,.2f} back above the CPR.", at)
        return None

    def on_news(self, impact: str, bias: str, title: str, at: datetime) -> Change | None:
        if impact == "high":
            self.stand_aside_until = at + timedelta(minutes=30)
            return self._set("STAND_ASIDE", f"High-impact news: {title[:90]}. No new trades for 30 minutes.", at)
        if (bias == "adverse" and self.stance == "LONG_BIAS") or (bias == "supportive" and self.stance == "SHORT_BIAS"):
            return self._set("NEUTRAL", f"News against the bias: {title[:90]}.", at)
        return None

    def on_proxy(self, label: str, change_pct: float, threshold_pct: float, at: datetime) -> Change | None:
        if abs(change_pct) >= threshold_pct:
            self.stand_aside_until = at + timedelta(minutes=30)
            return self._set("STAND_ASIDE", f"{label} moved {change_pct:+.2f}% since the open. No new trades for 30 minutes.", at)
        return None

    def on_guard(self, status: dict, at: datetime) -> Change | None:
        if not status.get("can_recommend", True):
            return self._set("DONE_FOR_DAY", "Risk guard: " + "; ".join(status.get("reasons", [])) + ".", at)
        return None
