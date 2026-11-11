"""CPR strategy backtester.

Tests CPR setups on historical 5-minute candles with realistic Zerodha charges,
slippage, conservative fills, and honest out-of-sample discipline:

  * levels for a session use only data from before that session
  * entry at the NEXT bar's open after a confirming close (no same-bar fills)
  * if stop and target are both inside one bar, the stop is assumed hit first
  * all positions are closed by 15:15 (intraday only)
  * data is split chronologically: in-sample 60%, out-of-sample 25%, and a
    final 15% holdout that stays hidden unless explicitly revealed
  * charges follow a dated schedule (STT changed on 1 Oct 2024 and 1 Apr 2026)

Index history from Kite has no volume and expired futures have no intraday
history, so trades are simulated on the index price with NIFTY/BANKNIFTY
FUTURES charges as the cost model. Futures basis is ignored. Option premiums
cannot be backtested with Kite data (no history for expired contracts).

    python pravin_backtest.py --id NIFTY50
    python pravin_backtest.py --id BANKNIFTY --reveal-holdout
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from pravin_quant import IST, classify_width, cpr, floor_pivots, regime

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"


# ====================================================================== charges
@dataclass(frozen=True)
class RateSet:
    effective_from: date
    futures_stt_sell: float      # fraction of sell turnover
    options_stt_sell: float      # fraction of sell premium turnover


# Securities Transaction Tax history (sell side). Verify at zerodha.com/charge-list.
STT_SCHEDULE = [
    RateSet(date(2000, 1, 1), 0.000125, 0.000625),
    RateSet(date(2024, 10, 1), 0.0002, 0.001),
    RateSet(date(2026, 4, 1), 0.0005, 0.0015),
]
FUT_TXN = 0.0000173      # NSE futures exchange transaction charge
OPT_TXN = 0.0003503      # NSE options exchange transaction charge (on premium)
SEBI = 0.000001          # ₹10 per crore
STAMP_FUT = 0.00002      # buy side
STAMP_OPT = 0.00003      # buy side
GST = 0.18               # on brokerage + exchange charges + SEBI fee
CHARGES_AS_OF = "2026-11-11 (Zerodha charge list; Budget 2026 STT effective 1 Apr 2026)"


def rates_on(d: date) -> RateSet:
    return [r for r in STT_SCHEDULE if r.effective_from <= d][-1]


def futures_charges(buy_value: float, sell_value: float, d: date) -> dict:
    """Round-trip charges for one futures trade (one buy order, one sell order)."""
    brok = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    stt = rates_on(d).futures_stt_sell * sell_value
    txn = FUT_TXN * (buy_value + sell_value)
    sebi = SEBI * (buy_value + sell_value)
    stamp = STAMP_FUT * buy_value
    gst = GST * (brok + txn + sebi)
    total = brok + stt + txn + sebi + stamp + gst
    return {"brokerage": brok, "stt": stt, "exchange": txn, "sebi": sebi, "stamp": stamp, "gst": gst, "total": total}


def options_charges(buy_premium_value: float, sell_premium_value: float, d: date) -> dict:
    brok = 40.0
    stt = rates_on(d).options_stt_sell * sell_premium_value
    txn = OPT_TXN * (buy_premium_value + sell_premium_value)
    sebi = SEBI * (buy_premium_value + sell_premium_value)
    stamp = STAMP_OPT * buy_premium_value
    gst = GST * (brok + txn + sebi)
    return {"brokerage": brok, "stt": stt, "exchange": txn, "sebi": sebi, "stamp": stamp, "gst": gst,
            "total": brok + stt + txn + sebi + stamp + gst}


# ====================================================================== context
@dataclass
class SessionContext:
    day: date
    bars: pd.DataFrame            # this session's 5-minute bars (IST index)
    prev: dict                    # previous session high/low/close
    cpr: object                   # daily CPR from previous session
    pivots: object
    width_label: str
    width_pct_rank: float | None
    weekly_cpr: object | None
    regime: str


def make_context(daily_before: pd.DataFrame, day: date, bars: pd.DataFrame) -> SessionContext:
    """Context for `day` from completed daily candles strictly before it.
    Used by both the backtester and the live watcher, so they cannot drift apart."""
    db = daily_before
    p = db.iloc[-1]
    lvl = cpr(p.high, p.low, p.close)
    widths = pd.Series([cpr(r.high, r.low, r.close).width_pct for r in db.iloc[:-1].itertuples()])
    wc = classify_width(widths.iloc[-250:], lvl.width_pct)
    wk = day.isocalendar()[:2]
    prior_weeks = [d for d in db.index if d.isocalendar()[:2] < wk]
    wcpr = None
    if prior_weeks:
        last_wk = prior_weeks[-1].isocalendar()[:2]
        g = db[[d.isocalendar()[:2] == last_wk for d in db.index]]
        wcpr = cpr(g.high.max(), g.low.min(), g.close.iloc[-1])
    return SessionContext(day, bars, {"high": p.high, "low": p.low, "close": p.close}, lvl,
                          floor_pivots(p.high, p.low, p.close), wc["label"], wc["percentile"], wcpr,
                          regime(db)["label"])


def _date_index(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    d.index = pd.Index([pd.Timestamp(x).date() for x in d.index])
    return d


def build_contexts(daily: pd.DataFrame, intraday: pd.DataFrame, warmup: int = 260) -> list[SessionContext]:
    """One context per session, built only from data before that session."""
    daily = _date_index(daily)
    idx = intraday.index.tz_convert(IST)
    by_day = {d: g for d, g in intraday.groupby(idx.date)}
    return [make_context(daily.iloc[:i], daily.index[i], by_day[daily.index[i]])
            for i in range(warmup, len(daily)) if daily.index[i] in by_day]


# ====================================================================== strategies
@dataclass(frozen=True)
class Setup:
    signal_bar: int      # index of the bar whose CLOSE confirmed the setup
    direction: int       # +1 long, -1 short
    stop: float
    target: float


@dataclass(frozen=True)
class Strategy:
    id: str
    version: str
    name: str
    hypothesis: str
    find: Callable[[SessionContext, dict], Setup | None]
    params: dict = field(default_factory=dict)
    param_grid: dict = field(default_factory=dict)


FIRST_SIGNAL = time(9, 30)     # ignore the first 15 minutes
LAST_SIGNAL = time(14, 30)
EXIT_TIME = time(15, 15)


def _times(bars):
    return [t.time() for t in bars.index.tz_convert(IST)]


def _opening_range(bars, minutes=15):
    t = _times(bars)
    m = [k for k, x in enumerate(t) if x < (datetime.combine(date.min, time(9, 15)) + pd.Timedelta(minutes=minutes)).time()]
    return bars["high"].iloc[m].max(), bars["low"].iloc[m].min()


def _rr_target(entry_ref, stop, direction, rr):
    return entry_ref + direction * rr * abs(entry_ref - stop)


def _is_narrow(ctx, p):
    return ctx.width_pct_rank is not None and ctx.width_pct_rank <= p["narrow_pct"]


def find_narrow_open_beyond(ctx: SessionContext, p: dict) -> Setup | None:
    """Narrow CPR and the session opens outside it; trade in the direction of
    the open after price confirms beyond the CPR and the opening range."""
    if not _is_narrow(ctx, p):
        return None
    b, c = ctx.bars, ctx.cpr
    o = b["open"].iloc[0]
    if c.bottom <= o <= c.top:
        return None
    direction = 1 if o > c.top else -1
    if p.get("need_weekly_confluence"):
        w = ctx.weekly_cpr
        if w is None or not (w.bottom <= c.pivot <= w.top):
            return None
    orh, orl = _opening_range(b)
    t = _times(b)
    for k in range(len(b)):
        if not (FIRST_SIGNAL <= t[k] <= LAST_SIGNAL):
            continue
        close = b["close"].iloc[k]
        if direction == 1 and close > max(c.top, orh):
            stop = c.bottom
            return Setup(k, 1, stop, _rr_target(close, stop, 1, p["rr"]))
        if direction == -1 and close < min(c.bottom, orl):
            stop = c.top
            return Setup(k, -1, stop, _rr_target(close, stop, -1, p["rr"]))
    return None


def find_inside_breakout(ctx: SessionContext, p: dict) -> Setup | None:
    """Session opens inside the CPR; trade the first 5-minute close outside it."""
    b, c = ctx.bars, ctx.cpr
    o = b["open"].iloc[0]
    if not (c.bottom <= o <= c.top):
        return None
    t = _times(b)
    for k in range(len(b)):
        if not (FIRST_SIGNAL <= t[k] <= LAST_SIGNAL):
            continue
        close = b["close"].iloc[k]
        if close > c.top:
            return Setup(k, 1, c.bottom, _rr_target(close, c.bottom, 1, p["rr"]))
        if close < c.bottom:
            return Setup(k, -1, c.top, _rr_target(close, c.top, -1, p["rr"]))
    return None


def find_cpr_rejection(ctx: SessionContext, p: dict) -> Setup | None:
    """Open above (below) the CPR, price tests into it and closes back out:
    trade the rejection with the stop beyond the far side of the CPR."""
    b, c = ctx.bars, ctx.cpr
    o = b["open"].iloc[0]
    if c.bottom <= o <= c.top:
        return None
    direction = 1 if o > c.top else -1
    t = _times(b)
    touched = False
    for k in range(len(b)):
        hi, lo, close = b["high"].iloc[k], b["low"].iloc[k], b["close"].iloc[k]
        if direction == 1:
            if close < c.bottom:
                return None                               # broke through: thesis invalid
            touched = touched or lo <= c.top
            if touched and close > c.top and FIRST_SIGNAL <= t[k] <= LAST_SIGNAL and lo <= c.top:
                stop = c.bottom - p["buffer_pct"] / 100 * c.pivot
                return Setup(k, 1, stop, _rr_target(close, stop, 1, p["rr"]))
        else:
            if close > c.top:
                return None
            touched = touched or hi >= c.bottom
            if touched and close < c.bottom and FIRST_SIGNAL <= t[k] <= LAST_SIGNAL and hi >= c.bottom:
                stop = c.top + p["buffer_pct"] / 100 * c.pivot
                return Setup(k, -1, stop, _rr_target(close, stop, -1, p["rr"]))
    return None


STRATEGIES = [
    Strategy("CPR-NARROW-OPEN", "1.0", "Narrow CPR, open outside, confirmed breakout",
             "Narrow CPR days trend; trade the side the market opens on once price clears the CPR and opening range.",
             find_narrow_open_beyond, {"narrow_pct": 20, "rr": 2.0}, {"narrow_pct": [20, 30, 40]}),
    Strategy("CPR-NARROW-WEEKLY", "1.0", "Narrow CPR inside weekly CPR, open outside",
             "As CPR-NARROW-OPEN, only when the daily pivot sits inside the weekly CPR (confluence).",
             find_narrow_open_beyond, {"narrow_pct": 30, "rr": 2.0, "need_weekly_confluence": True},
             {"narrow_pct": [20, 30, 40]}),
    Strategy("CPR-INSIDE-BREAK", "1.0", "Open inside CPR, trade the first break out",
             "An open inside the CPR is balance; the first 5-minute close outside it starts the move.",
             find_inside_breakout, {"rr": 2.0}, {"rr": [1.5, 2.0, 3.0]}),
    Strategy("CPR-REJECTION", "1.0", "CPR test and rejection",
             "When price opens away from the CPR, a test of the CPR that is rejected continues the open's direction.",
             find_cpr_rejection, {"rr": 2.0, "buffer_pct": 0.05}, {"rr": [1.5, 2.0, 3.0]}),
]


def get_strategy(sid: str) -> Strategy:
    for s in STRATEGIES:
        if s.id == sid:
            return s
    raise KeyError(sid)


# ====================================================================== simulation
@dataclass
class Trade:
    strategy: str
    day: date
    direction: int
    entry_time: str
    entry: float
    stop: float
    target: float
    exit_time: str
    exit: float
    exit_reason: str
    qty: int
    risk_points: float
    gross: float
    charges: float
    net: float
    r_gross: float
    r_net: float
    regime: str
    width_label: str


def simulate(ctx: SessionContext, s: Setup, strategy_id: str, qty: int, slippage: float) -> Trade | None:
    b = ctx.bars
    t = _times(b)
    k = s.signal_bar + 1
    if k >= len(b) or t[k] >= EXIT_TIME:
        return None
    d = s.direction
    entry = b["open"].iloc[k] + d * slippage
    risk = (entry - s.stop) * d
    if risk <= 0:
        return None                                       # gapped through the stop: no valid trade
    exit_px, reason, exit_k = None, None, None
    for j in range(k, len(b)):
        if t[j] >= EXIT_TIME:
            exit_px, reason, exit_k = b["open"].iloc[j] - d * slippage, "time", j
            break
        hi, lo = b["high"].iloc[j], b["low"].iloc[j]
        hit_stop = lo <= s.stop if d == 1 else hi >= s.stop
        hit_tgt = hi >= s.target if d == 1 else lo <= s.target
        if hit_stop:                                       # stop first when both are possible
            o = b["open"].iloc[j]
            gap_through = (o < s.stop) if d == 1 else (o > s.stop)
            exit_px = (o if gap_through else s.stop) - d * slippage
            reason, exit_k = "stop", j
            break
        if hit_tgt:
            exit_px, reason, exit_k = s.target - d * slippage, "target", j
            break
    if exit_px is None:
        exit_px, reason, exit_k = b["close"].iloc[-1] - d * slippage, "session_end", len(b) - 1
    gross = (exit_px - entry) * d * qty
    buy_px, sell_px = (entry, exit_px) if d == 1 else (exit_px, entry)
    ch = futures_charges(buy_px * qty, sell_px * qty, ctx.day)["total"]
    return Trade(strategy_id, ctx.day, d, str(b.index[k]), entry, s.stop, s.target, str(b.index[exit_k]), exit_px,
                 reason, qty, risk, gross, ch, gross - ch, (exit_px - entry) * d / risk, (gross - ch) / (risk * qty),
                 ctx.regime, ctx.width_label)


def run(strategy: Strategy, contexts: list[SessionContext], qty: int, slippage: float = 1.0,
        params: dict | None = None) -> list[Trade]:
    p = {**strategy.params, **(params or {})}
    trades = []
    for ctx in contexts:
        s = strategy.find(ctx, p)
        if s:
            tr = simulate(ctx, s, strategy.id, qty, slippage)
            if tr:
                trades.append(tr)
    return trades


# ====================================================================== metrics
def max_consecutive_losses(nets) -> int:
    best = cur = 0
    for x in nets:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def bootstrap_ci(values, n=2000, seed=7) -> tuple[float, float] | None:
    v = np.asarray(values, float)
    if len(v) < 20:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(v, (n, len(v)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def metrics(trades: list[Trade], sessions: int) -> dict:
    if not trades:
        return {"trades": 0, "sessions": sessions}
    net = np.array([t.net for t in trades])
    gross = np.array([t.gross for t in trades])
    rn = np.array([t.r_net for t in trades])
    wins, losses = net[net > 0], net[net < 0]
    eq = np.cumsum(net)
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])).max())
    daily = pd.Series(net, index=[t.day for t in trades]).groupby(level=0).sum()
    full = np.concatenate([daily.values, np.zeros(max(0, sessions - len(daily)))])
    sd = full.std(ddof=1) if len(full) > 1 else 0
    dn = full[full < 0].std(ddof=1) if (full < 0).sum() > 1 else 0
    return {
        "trades": len(trades), "sessions": sessions, "exposure_pct": len(trades) / sessions * 100 if sessions else None,
        "win_rate_pct": len(wins) / len(trades) * 100,
        "gross": float(gross.sum()), "charges": float(sum(t.charges for t in trades)), "net": float(net.sum()),
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
        "expectancy_rs": float(net.mean()), "expectancy_r": float(rn.mean()),
        "avg_r_gross": float(np.mean([t.r_gross for t in trades])),
        "expectancy_r_ci95": bootstrap_ci(rn),
        "max_drawdown_rs": dd, "max_consecutive_losses": max_consecutive_losses(net),
        "sharpe": float(full.mean() / sd * math.sqrt(252)) if sd > 0 else None,
        "sortino": float(full.mean() / dn * math.sqrt(252)) if dn > 0 else None,
        "exit_reasons": pd.Series([t.exit_reason for t in trades]).value_counts().to_dict(),
    }


def breakdown(trades: list[Trade], key: Callable[[Trade], str]) -> dict:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: {"trades": len(v), "net": float(sum(x.net for x in v)),
                "win_rate_pct": 100 * sum(x.net > 0 for x in v) / len(v),
                "expectancy_r": float(np.mean([x.r_net for x in v]))} for k, v in sorted(groups.items())}


# ====================================================================== validation
def split(contexts, is_frac=0.60, oos_frac=0.25):
    n = len(contexts)
    a, b = int(n * is_frac), int(n * (is_frac + oos_frac))
    return contexts[:a], contexts[a:b], contexts[b:]


def walk_forward(strategy: Strategy, contexts, qty, slippage, train=250, test=60, min_trades=10) -> dict:
    """Rolling: pick the parameter value with the best in-sample expectancy (R),
    then record its results on the next unseen window only."""
    if not strategy.param_grid:
        return {"windows": [], "trades": []}
    (pname, values), = strategy.param_grid.items()
    windows, oos_trades = [], []
    start = 0
    while start + train + test <= len(contexts):
        tr_ctx, te_ctx = contexts[start:start + train], contexts[start + train:start + train + test]
        scored = []
        for v in values:
            tt = run(strategy, tr_ctx, qty, slippage, {pname: v})
            if len(tt) >= min_trades:
                scored.append((np.mean([t.r_net for t in tt]), v))
        if scored:
            best = max(scored)[1]
            te = run(strategy, te_ctx, qty, slippage, {pname: best})
            oos_trades += te
            windows.append({"test_from": str(te_ctx[0].day), "test_to": str(te_ctx[-1].day), pname: best,
                            "trades": len(te), "net": float(sum(t.net for t in te))})
        start += test
    return {"param": pname, "windows": windows, "trades": oos_trades}


def verdict(oos: dict, slip2: dict) -> dict:
    """Backtest gate. Passing it makes a setup a CANDIDATE for paper trading,
    never 'approved' for real money."""
    n = oos.get("trades", 0)
    if n < 30:
        return {"status": "insufficient_sample", "reason": f"only {n} out-of-sample trades (need 30)"}
    ci = oos.get("expectancy_r_ci95")
    checks = {
        "profit_factor_above_1_2": (oos.get("profit_factor") or 0) > 1.2,
        "positive_expectancy_after_costs": oos["expectancy_r"] > 0,
        "survives_double_slippage": slip2.get("trades", 0) > 0 and slip2["expectancy_r"] > 0,
        "ci_lower_bound_above_zero": bool(ci and ci[0] > 0),
    }
    passed = all(checks.values())
    return {"status": "candidate_for_paper_trading" if passed else "rejected", "checks": checks}


def evaluate(strategy: Strategy, contexts, qty, slippage=1.0, reveal_holdout=False) -> dict:
    ins, oos, hold = split(contexts)
    t_in, t_oos = run(strategy, ins, qty, slippage), run(strategy, oos, qty, slippage)
    m_oos = metrics(t_oos, len(oos))
    sens = {f"{m}x": metrics(run(strategy, oos, qty, slippage * m), len(oos)).get("expectancy_r")
            for m in (0, 1, 2, 3)}
    slip2 = metrics(run(strategy, oos, qty, slippage * 2), len(oos))
    wf = walk_forward(strategy, ins + oos, qty, slippage)
    allt = t_in + t_oos
    res = {
        "strategy": {"id": strategy.id, "version": strategy.version, "name": strategy.name,
                     "hypothesis": strategy.hypothesis, "params": strategy.params},
        "periods": {"in_sample": [str(ins[0].day), str(ins[-1].day)] if ins else None,
                    "out_of_sample": [str(oos[0].day), str(oos[-1].day)] if oos else None,
                    "holdout": [str(hold[0].day), str(hold[-1].day)] if hold else None},
        "in_sample": metrics(t_in, len(ins)), "out_of_sample": m_oos,
        "slippage_sensitivity_expectancy_r": sens,
        "walk_forward": {"param": wf.get("param"), "windows": wf["windows"],
                         "metrics": metrics(wf["trades"], sum(1 for _ in wf["windows"]) * 60 or 1)},
        "by_year": breakdown(allt, lambda t: str(t.day.year)),
        "by_regime": breakdown(allt, lambda t: t.regime),
        "by_weekday": breakdown(allt, lambda t: t.day.strftime("%a")),
        "by_direction": breakdown(allt, lambda t: "long" if t.direction == 1 else "short"),
        "verdict": verdict(m_oos, slip2),
        "holdout": metrics(run(strategy, hold, qty, slippage), len(hold)) if reveal_holdout else "hidden",
    }
    return res


def backtest_instrument(daily, intraday, qty, slippage=1.0, reveal_holdout=False, strategies=STRATEGIES) -> dict:
    contexts = build_contexts(daily, intraday)
    if len(contexts) < 300:
        raise ValueError(f"Only {len(contexts)} usable sessions; need at least 300 (fetch more history).")
    return {"generated_at": datetime.now(IST).isoformat(), "sessions": len(contexts),
            "first_session": str(contexts[0].day), "last_session": str(contexts[-1].day),
            "assumptions": {"qty": qty, "slippage_points_per_side": slippage, "charges_as_of": CHARGES_AS_OF,
                            "instrument_model": "index price, futures charges, basis ignored, intraday only",
                            "fills": "next bar open; stop assumed before target within a bar; exit by 15:15"},
            "results": [evaluate(s, contexts, qty, slippage, reveal_holdout) for s in strategies]}


def to_markdown(iid: str, rep: dict) -> str:
    L = [f"# Backtest: {iid}", "",
         f"{rep['sessions']} sessions, {rep['first_session']} to {rep['last_session']}. "
         f"Qty {rep['assumptions']['qty']}, slippage {rep['assumptions']['slippage_points_per_side']} pt/side.",
         f"Charges as of {rep['assumptions']['charges_as_of']}.", "",
         "| Setup | Verdict | OOS trades | OOS win % | OOS PF | OOS exp. R | OOS net ₹ | Max DD ₹ |",
         "|---|---|---|---|---|---|---|---|"]
    f = lambda v, d=2: "—" if v is None else f"{v:,.{d}f}"
    for r in rep["results"]:
        o = r["out_of_sample"]
        L.append(f"| {r['strategy']['id']} | {r['verdict']['status']} | {o.get('trades', 0)} | "
                 f"{f(o.get('win_rate_pct'), 0)} | {f(o.get('profit_factor'))} | {f(o.get('expectancy_r'))} | "
                 f"{f(o.get('net'), 0)} | {f(o.get('max_drawdown_rs'), 0)} |")
    L += ["", "A 'candidate' still needs paper trading before real money. 'Rejected' means it lost money "
          "or failed a robustness check out of sample. Holdout results stay hidden until you choose to reveal them."]
    return "\n".join(L)


def latest_results(iid: str) -> dict | None:
    p = RESULTS_DIR / f"backtest_{iid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", default="NIFTY50")
    ap.add_argument("--lot-size", type=int, help="quantity per trade; defaults to the lot size saved by fetch")
    ap.add_argument("--slippage", type=float, default=1.0, help="index points per side")
    ap.add_argument("--reveal-holdout", action="store_true")
    a = ap.parse_args(argv)
    from pravin_kite import LocalStore
    st = LocalStore()
    qty = a.lot_size or st.meta().get("lot_sizes", {}).get(a.id)
    if not qty:
        raise SystemExit("Lot size unknown. Run `python pravin_kite.py fetch` (saves current lot sizes) or pass --lot-size.")
    rep = backtest_instrument(st.load(a.id, "day"), st.load(a.id, "5minute"), int(qty), a.slippage, a.reveal_holdout)
    RESULTS_DIR.mkdir(exist_ok=True)

    def conv(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        raise TypeError(type(o))
    (RESULTS_DIR / f"backtest_{a.id}.json").write_text(json.dumps(rep, default=conv, indent=1))
    md = to_markdown(a.id, rep)
    (RESULTS_DIR / f"backtest_{a.id}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
