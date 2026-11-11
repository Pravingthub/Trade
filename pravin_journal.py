"""Trade journal and paper trading.

Every trade, paper or real, is logged with its PLAN (setup, entry, stop,
target, size) before the result is known, then closed with what actually
happened. The review shows the things that hurt you before: overtrading,
late stop exits, trades outside validated setups, and what each cost in rupees.

Stored in journal.db (SQLite) on your computer. Never committed.

    python pravin_journal.py open --instrument NIFTY --kind option --side long \
        --entry 120 --stop 95 --target 170 --qty 65 --setup CPR-NARROW-OPEN --mode paper
    python pravin_journal.py close 1 --exit 168 --reason target
    python pravin_journal.py list
    python pravin_journal.py review --days 30
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from pravin_backtest import futures_charges, options_charges
from pravin_quant import IST, ClosedTrade, DailyGuard, RiskPolicy

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    instrument TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('option', 'future', 'equity')),
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    setup TEXT NOT NULL,                 -- strategy id, or 'discretionary'
    setup_validated INTEGER NOT NULL,    -- 1 if the setup had passed backtesting when opened
    from_alert INTEGER NOT NULL,         -- 1 if opened from a Vriddhi alert
    planned_entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL, qty INTEGER NOT NULL,
    planned_risk REAL NOT NULL,          -- rupees lost if the stop is honoured exactly
    guard_ok INTEGER NOT NULL,           -- 1 if the risk guard allowed a new trade at open time
    guard_reasons TEXT,
    opened_at TEXT NOT NULL, entry REAL NOT NULL,
    closed_at TEXT, exit REAL, exit_reason TEXT,
    gross REAL, charges REAL, net REAL, r_multiple REAL,
    notes TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def charges_for(kind: str, buy_value: float, sell_value: float, d: date) -> float:
    if kind == "option":
        return options_charges(buy_value, sell_value, d)["total"]
    if kind == "future":
        return futures_charges(buy_value, sell_value, d)["total"]
    # equity intraday (Zerodha): 0.03% or ₹20 per order, STT 0.025% sell, NSE txn 0.00297%, stamp 0.003% buy
    brok = min(20, 0.0003 * buy_value) + min(20, 0.0003 * sell_value)
    txn = 0.0000297 * (buy_value + sell_value)
    sebi = 0.000001 * (buy_value + sell_value)
    return brok + 0.00025 * sell_value + txn + sebi + 0.00003 * buy_value + 0.18 * (brok + txn + sebi)


def closed_today(con, day: date, mode: str) -> list[ClosedTrade]:
    rows = con.execute("SELECT closed_at, net, planned_risk FROM trades WHERE closed_at IS NOT NULL AND mode = ? "
                       "AND substr(closed_at, 1, 10) >= ?", (mode, (day - timedelta(days=7)).isoformat())).fetchall()
    return [ClosedTrade(datetime.fromisoformat(r["closed_at"]), r["net"], r["planned_risk"]) for r in rows]


def opened_count(con, day: date, mode: str) -> int:
    return con.execute("SELECT COUNT(*) FROM trades WHERE mode = ? AND substr(opened_at, 1, 10) = ?",
                       (mode, day.isoformat())).fetchone()[0]


def guard_for(con, now: datetime, policy: RiskPolicy, mode: str) -> DailyGuard:
    """The risk guard, rebuilt from the journal, so limits survive restarts."""
    closed = [t for t in closed_today(con, now.date(), mode) if t.closed_at <= now]   # never count the future
    return DailyGuard(policy, closed, opened_count(con, now.date(), mode))


class GuardBlocked(Exception):
    """The risk guard refused a new paper trade."""


def open_trade(con, *, mode, instrument, kind, side, entry, stop, target, qty, setup="discretionary",
               validated=False, from_alert=False, planned_entry=None, policy: RiskPolicy, now=None, notes="") -> int:
    """Paper trades are BLOCKED when the risk guard says stop. Live trades have
    already happened at the broker, so they are recorded and flagged instead."""
    now = now or datetime.now(IST)
    d = 1 if side == "long" else -1
    if (entry - stop) * d <= 0:
        raise ValueError("stop must be below entry for a long and above entry for a short")
    if (target - entry) * d <= 0:
        raise ValueError("target must be beyond entry in the trade's direction")
    status = guard_for(con, now, policy, mode).status(now)
    if mode == "paper" and not status["can_recommend"]:
        raise GuardBlocked("; ".join(status["reasons"]))
    cur = con.execute(
        "INSERT INTO trades (mode, instrument, kind, side, setup, setup_validated, from_alert, planned_entry, stop, target, qty,"
        " planned_risk, guard_ok, guard_reasons, opened_at, entry, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mode, instrument, kind, side, setup, int(validated), int(from_alert), planned_entry or entry, stop, target, qty,
         abs(entry - stop) * qty, int(status["can_recommend"]), "; ".join(status["reasons"]), now.isoformat(), entry, notes))
    con.commit()
    return cur.lastrowid


def close_trade(con, trade_id: int, exit_price: float, reason: str, now=None) -> dict:
    now = now or datetime.now(IST)
    t = con.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if t is None:
        raise KeyError(trade_id)
    if t["closed_at"]:
        raise ValueError(f"trade {trade_id} is already closed")
    d = 1 if t["side"] == "long" else -1
    gross = (exit_price - t["entry"]) * d * t["qty"]
    buy, sell = (t["entry"], exit_price) if d == 1 else (exit_price, t["entry"])
    ch = charges_for(t["kind"], buy * t["qty"], sell * t["qty"], now.date())
    net = gross - ch
    r = net / t["planned_risk"] if t["planned_risk"] else None
    con.execute("UPDATE trades SET closed_at=?, exit=?, exit_reason=?, gross=?, charges=?, net=?, r_multiple=? WHERE id=?",
                (now.isoformat(), exit_price, reason, gross, ch, net, r, trade_id))
    con.commit()
    return dict(con.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())


# ====================================================================== review
def review(con, since: date, mode: str | None = None) -> dict:
    q = "SELECT * FROM trades WHERE substr(opened_at, 1, 10) >= ?" + (" AND mode = ?" if mode else "")
    rows = [dict(r) for r in con.execute(q, (since.isoformat(), mode) if mode else (since.isoformat(),))]
    closed = [r for r in rows if r["closed_at"]]
    if not closed:
        return {"since": since.isoformat(), "trades": len(rows), "closed": 0, "open": len(rows)}
    net = np.array([r["net"] for r in closed])
    tol = 0.05                                   # 5% beyond the planned risk counts as a late exit
    late = [r for r in closed if r["net"] < 0 and -(r["gross"]) > r["planned_risk"] * (1 + tol)]
    late_cost = sum(-(r["gross"]) - r["planned_risk"] for r in late)
    off_plan = [r for r in closed if not r["setup_validated"]]
    over_guard = [r for r in closed if not r["guard_ok"]]
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["opened_at"][:10], []).append(r)

    def grp(key):
        g: dict[str, list] = {}
        for r in closed:
            g.setdefault(key(r), []).append(r)
        return {k: {"trades": len(v), "net": sum(x["net"] for x in v),
                    "win_rate_pct": 100 * sum(x["net"] > 0 for x in v) / len(v),
                    "avg_r": float(np.mean([x["r_multiple"] for x in v if x["r_multiple"] is not None] or [0]))}
                for k, v in sorted(g.items())}
    wins, losses = net[net > 0], net[net < 0]
    return {
        "since": since.isoformat(), "trades": len(rows), "closed": len(closed), "open": len(rows) - len(closed),
        "net": float(net.sum()), "charges": float(sum(r["charges"] for r in closed)),
        "win_rate_pct": 100 * len(wins) / len(closed),
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
        "avg_r": float(np.mean([r["r_multiple"] for r in closed])),
        "discipline": {
            "late_stop_exits": len(late), "late_stop_cost": float(late_cost),
            "off_plan_trades": len(off_plan), "off_plan_net": float(sum(r["net"] for r in off_plan)),
            "trades_after_guard_said_stop": len(over_guard), "after_guard_net": float(sum(r["net"] for r in over_guard)),
            "days_over_trade_limit": sum(1 for v in by_day.values() if len(v) > 3),
        },
        "by_setup": grp(lambda r: r["setup"]), "by_mode": grp(lambda r: r["mode"]),
        "by_kind": grp(lambda r: r["kind"]),
    }


def review_markdown(rv: dict) -> str:
    if not rv.get("closed"):
        return f"No closed trades since {rv['since']}."
    f = lambda v, d=0: "—" if v is None else f"{v:,.{d}f}"
    d = rv["discipline"]
    L = [f"# Journal review since {rv['since']}", "",
         f"{rv['closed']} closed trades ({rv['open']} open). Net ₹{f(rv['net'])} after ₹{f(rv['charges'])} charges. "
         f"Win rate {f(rv['win_rate_pct'])}%, profit factor {f(rv['profit_factor'], 2)}, average {f(rv['avg_r'], 2)}R.", "",
         "## Discipline", "",
         f"- Late stop exits: {d['late_stop_exits']}, costing ₹{f(d['late_stop_cost'])} beyond the planned stops.",
         f"- Trades outside validated setups: {d['off_plan_trades']}, net ₹{f(d['off_plan_net'])}.",
         f"- Trades taken after the risk guard said stop: {d['trades_after_guard_said_stop']}, net ₹{f(d['after_guard_net'])}.",
         f"- Days with more than 3 trades: {d['days_over_trade_limit']}.", "", "## By setup", "",
         "| Setup | Trades | Net ₹ | Win % | Avg R |", "|---|---|---|---|---|"]
    L += [f"| {k} | {v['trades']} | {f(v['net'])} | {f(v['win_rate_pct'])} | {f(v['avg_r'], 2)} |" for k, v in rv["by_setup"].items()]
    return "\n".join(L)


# ====================================================================== paper trading engine
@dataclass
class PaperPosition:
    trade_id: int
    side: int
    stop: float
    target: float


class PaperBook:
    """Simulated execution for alerts, using the same rules as the backtester:
    entry at the next bar's open, stop assumed before target inside a bar,
    gap-through fills at the open, flat by 15:15."""

    def __init__(self, con, policy: RiskPolicy, slippage: float = 0.0):
        self.con, self.policy, self.slippage = con, policy, slippage
        self.pending: list[dict] = []
        self.open: list[PaperPosition] = []

    def queue(self, instrument, side, stop, target, qty, setup, validated, kind="future"):
        self.pending.append(dict(instrument=instrument, side=side, stop=stop, target=target, qty=qty,
                                 setup=setup, validated=validated, kind=kind))

    def on_bar(self, bar: dict) -> list[str]:
        msgs = []
        ts, o, h, lo = bar["ts"], bar["open"], bar["high"], bar["low"]
        if ts.astimezone(IST).time() >= datetime.strptime("15:15", "%H:%M").time():
            for p in list(self.open):
                px = o - p.side * self.slippage
                close_trade(self.con, p.trade_id, px, "time", ts)
                self.open.remove(p)
                msgs.append(f"Paper #{p.trade_id} closed at 15:15 ({px:,.2f})")
            self.pending.clear()
            return msgs
        for q in self.pending:
            d = 1 if q["side"] == "long" else -1
            entry = o + d * self.slippage
            if (entry - q["stop"]) * d <= 0:
                msgs.append(f"Paper {q['setup']} skipped: opened through the stop")
                continue
            try:
                tid = open_trade(self.con, mode="paper", instrument=q["instrument"], kind=q["kind"], side=q["side"],
                                 entry=entry, stop=q["stop"], target=q["target"], qty=q["qty"], setup=q["setup"],
                                 validated=q["validated"], from_alert=True, policy=self.policy, now=ts)
            except GuardBlocked as e:
                msgs.append(f"Paper {q['setup']} blocked by the risk guard: {e}")
                continue
            self.open.append(PaperPosition(tid, d, q["stop"], q["target"]))
            msgs.append(f"Paper #{tid} opened {q['side']} at {entry:,.2f}")
        self.pending.clear()
        for p in list(self.open):
            stop_hit = lo <= p.stop if p.side == 1 else h >= p.stop
            tgt_hit = h >= p.target if p.side == 1 else lo <= p.target
            if stop_hit:
                gap = o < p.stop if p.side == 1 else o > p.stop
                px = (o if gap else p.stop) - p.side * self.slippage
                close_trade(self.con, p.trade_id, px, "stop", ts)
            elif tgt_hit:
                px = p.target - p.side * self.slippage
                close_trade(self.con, p.trade_id, px, "target", ts)
            else:
                continue
            self.open.remove(p)
            msgs.append(f"Paper #{p.trade_id} closed: {'stop' if stop_hit else 'target'} at {px:,.2f}")
        return msgs


# ====================================================================== CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, help="defaults to vriddhi_config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    for a in ("--instrument", "--kind", "--side"):
        o.add_argument(a, required=True)
    for a in ("--entry", "--stop", "--target"):
        o.add_argument(a, type=float, required=True)
    o.add_argument("--qty", type=int, required=True)
    o.add_argument("--setup", default="discretionary")
    o.add_argument("--validated", action="store_true", help="the setup had passed backtesting")
    o.add_argument("--mode", default="paper", choices=["paper", "live"])
    o.add_argument("--notes", default="")
    c = sub.add_parser("close")
    c.add_argument("id", type=int)
    c.add_argument("--exit", type=float, required=True)
    c.add_argument("--reason", default="manual", choices=["target", "stop", "time", "manual", "trail"])
    sub.add_parser("list")
    r = sub.add_parser("review")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--mode", choices=["paper", "live"])
    a = ap.parse_args(argv)
    con = connect()
    import pravin_config
    pol = pravin_config.risk_policy(capital=a.capital)
    if a.cmd == "open":
        before = guard_for(con, datetime.now(IST), pol, a.mode).status(datetime.now(IST))
        if not before["can_recommend"]:
            print("WARNING - risk guard says no new trades: " + "; ".join(before["reasons"]))
            print("The trade is logged anyway, and the review will show it as taken against the guard.")
        try:
            tid = open_trade(con, mode=a.mode, instrument=a.instrument, kind=a.kind, side=a.side, entry=a.entry, stop=a.stop,
                             target=a.target, qty=a.qty, setup=a.setup, validated=a.validated, policy=pol, notes=a.notes)
        except GuardBlocked as e:
            raise SystemExit(f"Paper trade refused by the risk guard: {e}") from None
        risk = abs(a.entry - a.stop) * a.qty
        print(f"Opened #{tid}. Planned risk ₹{risk:,.0f} ({risk / pol.risk_budget:.1f}x your ₹{pol.risk_budget:,.0f} budget). "
              "Place the stop order in Kite now.")
    elif a.cmd == "close":
        t = close_trade(con, a.id, a.exit, a.reason)
        print(f"Closed #{a.id}: net ₹{t['net']:,.0f} ({t['r_multiple']:.2f}R) after ₹{t['charges']:,.0f} charges.")
    elif a.cmd == "list":
        for t in con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 30"):
            state = f"net ₹{t['net']:,.0f}" if t["closed_at"] else "OPEN"
            print(f"#{t['id']} {t['opened_at'][:16]} {t['mode']} {t['instrument']} {t['side']} {t['setup']} {state}")
    elif a.cmd == "review":
        print(review_markdown(review(con, date.today() - timedelta(days=a.days), a.mode)))


if __name__ == "__main__":
    main()
