"""Single source of settings for every Vriddhi tool (vriddhi_config.json).

Risk limits are validated here so no tool, and no AI summary, can run with
limits looser than the ones you set.
"""
from __future__ import annotations

import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from pravin_quant import RiskPolicy

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "vriddhi_config.json"


class ConfigError(ValueError):
    pass


def load(path: Path | None = None) -> dict:
    p = path or CONFIG_PATH
    if not p.exists():
        raise ConfigError(f"{p.name} not found")
    return json.loads(p.read_text())


def risk_policy(cfg: dict | None = None, capital: float | None = None) -> RiskPolicy:
    r = (cfg or load())["risk"]
    cap = capital if capital is not None else r["capital"]
    lo, hi = r.get("allowed_capital", [50_000, 100_000])
    if not (lo <= cap <= hi):
        raise ConfigError(f"capital ₹{cap:,.0f} is outside your allowed range ₹{lo:,.0f}–₹{hi:,.0f}")
    checks = [(0 < r["risk_per_trade_pct"] <= 2, "risk_per_trade_pct must be in (0, 2]"),
              (0 < r["daily_loss_stop_pct"] <= 5, "daily_loss_stop_pct must be in (0, 5]"),
              (r["daily_loss_stop_pct"] <= r["weekly_loss_stop_pct"] <= 10, "weekly stop must be >= daily stop and <= 10%"),
              (1 <= r["max_trades_per_day"] <= 5, "max_trades_per_day must be 1-5"),
              (r["min_reward_risk"] >= 1, "min_reward_risk must be >= 1")]
    for ok, msg in checks:
        if not ok:
            raise ConfigError(msg)
    return RiskPolicy(capital=cap, risk_per_trade_pct=r["risk_per_trade_pct"], daily_loss_stop_pct=r["daily_loss_stop_pct"],
                      weekly_loss_stop_pct=r["weekly_loss_stop_pct"], max_trades_per_day=r["max_trades_per_day"],
                      min_reward_risk=r["min_reward_risk"], cooldown_after_loss_min=r["cooldown_after_loss_min"],
                      consecutive_loss_lockout=r["consecutive_loss_lockout"])


def policy_summary(p: RiskPolicy, trades_opened_today: int = 0) -> dict:
    """Every risk number the UI shows, computed here (never in the browser)."""
    return {"capital": p.capital, "per_trade": p.risk_budget, "daily_stop": p.capital * p.daily_loss_stop_pct / 100,
            "weekly_stop": p.capital * p.weekly_loss_stop_pct / 100, "max_trades": p.max_trades_per_day,
            "trades_left": max(0, p.max_trades_per_day - trades_opened_today), "min_reward_risk": p.min_reward_risk,
            "cooldown_min": p.cooldown_after_loss_min, "loss_lockout": p.consecutive_loss_lockout}


@dataclass(frozen=True)
class Contract:
    instrument: str
    lot_size: int | None
    lot_verified: bool
    strike_step: int
    expiry_rule: str


def contract(iid: str, cfg: dict | None = None, store_meta: dict | None = None) -> Contract | None:
    c = (cfg or load())["contracts"].get(iid)
    if not c:
        return None
    live_lot = (store_meta or {}).get("lot_sizes", {}).get(iid)
    return Contract(iid, live_lot or c.get("lot_size"), live_lot is not None, c["strike_step"], c["expiry_rule"])


def next_expiry(rule: str, after: date, is_session) -> date:
    """Expiry by rule, moved to the previous trading day if it falls on a holiday.
    Rules are assumptions until checked against the exchange's contract list."""
    if rule == "weekly_tuesday":
        d = after + timedelta(days=(1 - after.weekday()) % 7)
        cands = [d, d + timedelta(days=7)]
    elif rule == "monthly_last_tuesday":
        cands = []
        y, m = after.year, after.month
        for _ in range(2):
            last = date(y, m, monthrange(y, m)[1])
            cands.append(last - timedelta(days=(last.weekday() - 1) % 7))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        raise ConfigError(f"unknown expiry rule {rule}")
    for d in cands:
        e = d
        while not is_session(e):
            e -= timedelta(days=1)
        if e >= after:
            return e
    raise ConfigError("no expiry found")
