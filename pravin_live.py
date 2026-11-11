"""Live market watcher: builds 5-minute candles from Zerodha ticks, watches
CPR/pivot levels, fires setup alerts for VALIDATED setups only, enforces the
daily risk guard, and pauses everything when the feed goes stale.

READ-ONLY: it never places orders. You decide and execute in Kite.

    python pravin_live.py                     # NIFTY 50 + NIFTY BANK, Telegram alerts
    python pravin_live.py --show-unvalidated  # also show setups that have not passed backtests (labelled)
    python pravin_live.py --paper             # paper-trade each alerted setup into the journal

Needs: a Kite Connect subscription, `python pravin_kite.py login` today,
local daily history (`python pravin_kite.py fetch`), and TELEGRAM_* in .env
for phone alerts (otherwise alerts print to the terminal).
"""
from __future__ import annotations

import argparse
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

import pandas as pd

import pravin_backtest as B
from pravin_quant import IST, ClosedTrade, DailyGuard, RiskPolicy, position_size
from pravin_stance import StanceEngine

OPEN, CLOSE = time(9, 15), time(15, 30)


# ====================================================================== candles
class CandleBuilder:
    """Turns (timestamp, last price) ticks into 5-minute OHLC bars anchored to
    09:15 IST. A bar is emitted once a tick from a later bucket arrives or the
    clock passes the bucket end (flush)."""

    def __init__(self, minutes: int = 5):
        self.step = timedelta(minutes=minutes)
        self.cur: dict | None = None
        self.done: list[dict] = []

    def _bucket(self, ts: datetime) -> datetime:
        ts = ts.astimezone(IST)
        start = ts.replace(hour=9, minute=15, second=0, microsecond=0)
        return start + ((ts - start) // self.step) * self.step

    def on_tick(self, ts: datetime, price: float) -> list[dict]:
        t = ts.astimezone(IST).time()
        if not (OPEN <= t < CLOSE):
            return []
        b = self._bucket(ts)
        out = []
        if self.cur and b > self.cur["ts"]:
            out.append(self._close())
        if not self.cur:
            self.cur = {"ts": b, "open": price, "high": price, "low": price, "close": price, "ticks": 0}
        c = self.cur
        c["high"], c["low"], c["close"], c["ticks"] = max(c["high"], price), min(c["low"], price), price, c["ticks"] + 1
        return out

    def flush(self, now: datetime) -> list[dict]:
        if self.cur and now.astimezone(IST) >= self.cur["ts"] + self.step:
            return [self._close()]
        return []

    def _close(self) -> dict:
        bar, self.cur = self.cur, None
        self.done.append(bar)
        return bar

    def frame(self) -> pd.DataFrame:
        if not self.done:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(self.done).set_index("ts")[["open", "high", "low", "close"]]
        df.index = pd.DatetimeIndex(df.index)
        return df.assign(volume=0.0)


# ====================================================================== alerts
@dataclass
class Alert:
    key: str
    kind: str          # level | setup | feed | guard
    text: str
    at: datetime


@dataclass
class Watcher:
    iid: str
    name: str
    daily_before: pd.DataFrame
    day: date
    validated: set[str]
    guard: DailyGuard
    lot_size: int | None = None
    show_unvalidated: bool = False
    stale_after: timedelta = timedelta(seconds=90)
    builder: CandleBuilder = field(default_factory=CandleBuilder)
    sent: set[str] = field(default_factory=set)
    last_tick: datetime | None = None
    stale: bool = False
    paper: object | None = None            # pravin_journal.PaperBook, to paper-trade alerts automatically
    guard_fn: Callable | None = None       # returns a DailyGuard rebuilt from the journal
    stance: StanceEngine | None = None     # intraday stance; setup alerts must agree with it
    opened: bool = False

    def __post_init__(self):
        self.ctx = B.make_context(self.daily_before, self.day, pd.DataFrame())
        c, f = self.ctx.cpr, self.ctx.pivots
        p = self.ctx.prev
        self.levels = {"CPR top": c.top, "CPR bottom": c.bottom, "R1": f.r1, "S1": f.s1, "R2": f.r2, "S2": f.s2,
                       "PDH": p["high"], "PDL": p["low"]}
        self.prev_close = None
        if self.stance is None:
            self.stance = StanceEngine(self.name, c.top, c.bottom)

    def _emit(self, key, kind, text, at) -> list[Alert]:
        if key in self.sent:
            return []
        self.sent.add(key)
        return [Alert(key, kind, f"{self.name}: {text}", at)]

    def _stance_alert(self, ch) -> list[Alert]:
        return [Alert(f"stance-{ch.at}-{ch.new}", "stance", ch.text(self.name), datetime.fromisoformat(ch.at))] if ch else []

    def on_news(self, impact: str, bias: str, title: str, at: datetime) -> list[Alert]:
        return self._stance_alert(self.stance.on_news(impact, bias, title, at))

    def on_proxy(self, label: str, change_pct: float, threshold_pct: float, at: datetime) -> list[Alert]:
        return self._stance_alert(self.stance.on_proxy(label, change_pct, threshold_pct, at))

    def on_tick(self, ts: datetime, price: float) -> list[Alert]:
        out = []
        if not self.opened and OPEN <= ts.astimezone(IST).time() < CLOSE:
            self.opened = True
            out += self._stance_alert(self.stance.on_open(price, ts))
        if self.stale:
            self.stale = False
            out += [Alert(f"feed-ok-{ts:%H%M%S}", "feed", f"{self.name}: data feed is back. Alerts resumed.", ts)]
        self.last_tick = ts
        for bar in self.builder.on_tick(ts, price):
            out += self.on_bar(bar)
        return out

    def check_clock(self, now: datetime) -> list[Alert]:
        out = []
        for bar in self.builder.flush(now):
            out += self.on_bar(bar)
        t = now.astimezone(IST).time()
        since = self.last_tick or datetime.combine(self.day, OPEN, IST)     # no tick yet: measure from the open
        if OPEN <= t < CLOSE and now - since > self.stale_after and not self.stale:
            self.stale = True
            what = f"no ticks for {int((now - since).total_seconds())}s" if self.last_tick else "no ticks received since the open"
            out.append(Alert(f"stale-{now:%H%M}", "feed", f"{self.name}: {what}. Feed is stale: all setup alerts are paused.", now))
        return out

    def on_bar(self, bar: dict) -> list[Alert]:
        out = []
        close, at = bar["close"], bar["ts"]
        if self.prev_close is not None:
            for name, lvl in self.levels.items():
                if self.prev_close <= lvl < close:
                    out += self._emit(f"{name}-up", "level", f"5-min close {close:,.2f} above {name} {lvl:,.2f}", at)
                elif self.prev_close >= lvl > close:
                    out += self._emit(f"{name}-down", "level", f"5-min close {close:,.2f} below {name} {lvl:,.2f}", at)
        self.prev_close = close
        if self.paper is not None:
            out += [Alert(f"paper-{at:%H%M}-{k}", "paper", f"{self.name}: {m}", at)
                    for k, m in enumerate(self.paper.on_bar(bar))]
        out += self._stance_alert(self.stance.on_bar(close, at))
        g = (self.guard_fn(at) if self.guard_fn else self.guard).status(at)
        out += self._stance_alert(self.stance.on_guard(g, at))
        if not self.stale:
            out += self._setups(at)
        return out

    def _setups(self, at: datetime) -> list[Alert]:
        bars = self.builder.frame()
        ctx = B.SessionContext(self.day, bars, self.ctx.prev, self.ctx.cpr, self.ctx.pivots,
                               self.ctx.width_label, self.ctx.width_pct_rank, self.ctx.weekly_cpr, self.ctx.regime)
        out = []
        for s in B.STRATEGIES:
            ok = s.id in self.validated
            if not ok and not self.show_unvalidated:
                continue
            setup = s.find(ctx, s.params)
            if not setup or setup.signal_bar != len(bars) - 1:
                continue                                   # only alert on the bar that just confirmed
            st = self.stance.stance
            if st in ("STAND_ASIDE", "DONE_FOR_DAY") or (st == "LONG_BIAS" and setup.direction == -1) \
                    or (st == "SHORT_BIAS" and setup.direction == 1):
                continue                                   # the stance overrides: no alert against it
            side = "LONG" if setup.direction == 1 else "SHORT"
            ref = bars["close"].iloc[-1]
            gs = (self.guard_fn(at) if self.guard_fn else self.guard).status(at)
            sz = None
            lines = [f"{s.name} confirmed. {side} above/below {ref:,.2f} on the next bar.",
                     f"Stop {setup.stop:,.2f} · target {setup.target:,.2f} ({s.params.get('rr', 2):g}R).",
                     f"Underlying risk {abs(ref - setup.stop):,.2f} points."]
            if self.lot_size:
                sz = position_size(self.guard.policy, ref, setup.stop, self.lot_size, "long" if setup.direction == 1 else "short")
                lines.append(f"Size: {sz.lots} lot(s), ₹{sz.rupee_risk:,.0f} risk." if sz.ok else f"Size: NO TRADE ({sz.reason}).")
            if not gs["can_recommend"]:
                lines.insert(0, "RISK GUARD: no new trades (" + "; ".join(gs["reasons"]) + ").")
            if not ok:
                lines.insert(0, "NOT VALIDATED (information only).")
            lines.append("Options: check strike liquidity, spread and time decay before acting.")
            emitted = self._emit(f"setup-{s.id}", "setup", " ".join(lines), at)
            if emitted and self.paper is not None and gs["can_recommend"] and sz is not None and sz.ok:
                self.paper.queue(self.iid, "long" if setup.direction == 1 else "short", setup.stop, setup.target,
                                 sz.quantity, s.id, ok)
                emitted[0].text += " Paper trade queued for the next bar."
            out += emitted
        return out


# ====================================================================== runner (Kite WebSocket)
def run_live(ids: list[str], show_unvalidated: bool, capital: float, notify: Callable[[str], None], paper: bool = False):
    from kiteconnect import KiteTicker

    from pravin_kite import DEFAULT_INSTRUMENTS, LocalStore, kite_client, load_env, load_token, resolve_tokens
    env = load_env()
    kite = kite_client(env)
    store = LocalStore()
    tokens = resolve_tokens(kite, {i: DEFAULT_INSTRUMENTS[i] for i in ids})
    today = datetime.now(IST).date()
    import pravin_journal as J
    policy = RiskPolicy(capital=capital)
    guard = DailyGuard(policy)
    con = J.connect()
    mode = "paper" if paper else "live"
    book = J.PaperBook(con, policy, slippage=1.0) if paper else None

    def guard_fn(now):
        return J.guard_for(con, now, policy, mode)
    lots = store.meta().get("lot_sizes", {})
    watchers = {}
    for iid, tok in tokens.items():
        daily = B._date_index(store.load(iid, "day"))
        daily = daily[daily.index < today]
        rep = B.latest_results(iid) or {"results": []}
        validated = {r["strategy"]["id"] for r in rep["results"] if r["verdict"]["status"] == "candidate_for_paper_trading"}
        watchers[tok] = Watcher(iid, DEFAULT_INSTRUMENTS[iid][1], daily, today, validated, guard, lots.get(iid), show_unvalidated,
                                paper=book, guard_fn=guard_fn)
        notify(f"Watching {DEFAULT_INSTRUMENTS[iid][1]}: CPR {watchers[tok].ctx.cpr.bottom:,.2f}–{watchers[tok].ctx.cpr.top:,.2f}; "
               f"validated setups: {', '.join(sorted(validated)) or 'none'}")
    from pravin_kite import resolve_proxies
    proxies = resolve_proxies(kite)
    proxy_by_token = {v["token"]: (k, v) for k, v in proxies.items()}
    proxy_open: dict[int, float] = {}
    notify("Watching proxies: " + (", ".join(f"{k} ({v['symbol']})" for k, v in proxies.items()) or "none found"))
    kt = KiteTicker(env["KITE_API_KEY"], load_token())
    lock = threading.Lock()

    def poll_news():
        import pravin_news as N
        con = N.connect()
        seen = {r[0] for r in con.execute("SELECT id FROM news")}
        while datetime.now(IST).time() < time(15, 30):
            try:
                N.fetch_all(con)
                fresh = [dict(r) for r in con.execute("SELECT * FROM news") if r["id"] not in seen]
                seen |= {r["id"] for r in fresh}
                with lock:
                    for item in fresh:
                        if item["impact"] in ("high", "medium"):
                            notify(f"News [{item['impact']}/{item['bias']}]: {item['publisher']}: {item['title']}")
                        for w in watchers.values():
                            if not item["affects"] or w.iid in item["affects"]:
                                for a in w.on_news(item["impact"], item["bias"] or "", item["title"], datetime.now(IST)):
                                    notify(a.text)
            except Exception as e:                  # news problems never stop price monitoring
                print("news poll failed:", e)
            _time.sleep(300)

    threading.Thread(target=poll_news, daemon=True).start()

    def on_ticks(ws, ticks):
        with lock:
            for t in ticks:
                pr = proxy_by_token.get(t["instrument_token"])
                if pr:
                    px = float(t["last_price"])
                    base = proxy_open.setdefault(t["instrument_token"], px)
                    chg = (px / base - 1) * 100
                    for w in watchers.values():
                        for a in w.on_proxy(pr[0], chg, pr[1]["threshold_pct"], datetime.now(IST)):
                            notify(a.text)
                    continue
                w = watchers.get(t["instrument_token"])
                if w:
                    for a in w.on_tick(datetime.now(IST), float(t["last_price"])):
                        notify(a.text)

    def on_connect(ws, response):
        toks = list(watchers) + list(proxy_by_token)
        ws.subscribe(toks)
        ws.set_mode(ws.MODE_LTP, toks)

    def on_close(ws, code, reason):
        print(f"WebSocket closed ({code}): {reason}")

    def on_error(ws, code, reason):
        print(f"WebSocket error ({code}): {reason}")

    kt.on_ticks, kt.on_connect, kt.on_close, kt.on_error = on_ticks, on_connect, on_close, on_error
    kt.connect(threaded=True)
    try:
        while datetime.now(IST).time() < time(15, 31):
            _time.sleep(5)
            with lock:
                for w in watchers.values():
                    for a in w.check_clock(datetime.now(IST)):
                        notify(a.text)
    finally:
        kt.close()
        notify("Market closed. Live watch stopped.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", default="NIFTY50,BANKNIFTY")
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--show-unvalidated", action="store_true")
    ap.add_argument("--paper", action="store_true", help="paper-trade every alerted setup into journal.db")
    a = ap.parse_args(argv)
    from pravin_brief import telegram_from_env
    from pravin_kite import load_env
    tg = telegram_from_env(load_env())

    def notify(text):
        print(datetime.now(IST).strftime("%H:%M:%S"), text)
        if tg:
            try:
                tg.send(text, dedupe_key=f"{date.today()}-{text}")
            except Exception as e:      # alerts must never crash the watcher
                print("Telegram failed:", e)
    run_live(a.ids.split(","), a.show_unvalidated, a.capital, notify, a.paper)


__all__ = ["CandleBuilder", "Watcher", "Alert", "ClosedTrade"]

if __name__ == "__main__":
    main()
