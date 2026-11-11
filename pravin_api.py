"""Vriddhi API (single module): provider interface, DEMO data provider,
service layer and FastAPI routes. Read-only analytics; no order routes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import Literal, Protocol

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

import pravin_quant as ind
from pravin_quant import (
    ExchangeCalendar,
    QuantError,
    RiskPolicy,
    aggregate,
    camarilla,
    classify_width,
    cpr,
    cpr_history,
    cpr_relation,
    daily_from_intraday,
    floor_pivots,
    freshness,
    missing_sessions,
    month_key,
    opening_range,
    position_size,
    previous_completed,
    price_position,
    regime,
    resample_intraday,
    reward_risk,
    timeframe_summary,
    validate_candles,
    week_key,
)


# ======================================================================
# base
# ======================================================================
# Provider interface. The app never talks to Zerodha (or anyone) directly;
# it talks to a MarketDataProvider. Zerodha arrives in Milestone 5 as
# ZerodhaKiteProvider behind a feature flag.
class DataMode(str, Enum):
    LIVE = "live"
    DELAYED = "delayed"
    HISTORICAL = "historical"
    SIMULATED = "simulated"
    MANUAL = "manual"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Instrument:
    id: str
    symbol: str
    name: str
    exchange: str
    segment: str            # index | equity | future
    has_volume: bool
    tick_size: float
    lot_size: int | None    # None for indices; never hardcode for real contracts
    volume_proxy: str | None = None   # instrument whose volume backs VWAP

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Provenance:
    provider: str
    data_mode: DataMode
    interval: str
    first_bar: str
    last_bar: str
    ingested_at: str
    calendar_source: str
    validation: str

    def to_dict(self):
        d = asdict(self)
        d["data_mode"] = self.data_mode.value
        d["demo"] = self.data_mode == DataMode.SIMULATED
        return d


class MarketDataProvider(Protocol):
    name: str
    data_mode: DataMode

    def get_instruments(self) -> list[Instrument]: ...
    def get_historical_candles(self, instrument_id: str, interval: str,
                               start: date | None = None, end: date | None = None) -> pd.DataFrame: ...
    def last_update(self, instrument_id: str) -> datetime: ...


# ======================================================================
# fixture
# ======================================================================
# Deterministic SIMULATED market data for development and tests.
#
# These are invented prices from a seeded random process. They are not NSE data
# and must always be displayed with a DEMO label. Daily bars are built from the
# 5-minute bars so every timeframe is internally consistent.
# Demo calendar: weekends plus three fixed national holidays. Production must
# load the official NSE holiday circular for each year.
def demo_calendar(years=(2024, 2025, 2026)) -> ExchangeCalendar:
    hol = set()
    for y in years:
        hol |= {date(y, 1, 26), date(y, 8, 15), date(y, 10, 2)}
    return ExchangeCalendar(holidays=frozenset(hol), source="DEMO calendar (weekends + 26 Jan, 15 Aug, 2 Oct)")


INSTRUMENTS = [
    Instrument("NIFTY50", "NIFTY 50", "NIFTY 50 index (demo)", "NSE", "index", False, 0.05, None, "NIFTYFUT"),
    Instrument("BANKNIFTY", "NIFTY BANK", "NIFTY BANK index (demo)", "NSE", "index", False, 0.05, None),
    Instrument("NIFTYFUT", "NIFTY FUT", "NIFTY near-month future (demo)", "NFO", "future", True, 0.10, None),
    Instrument("RELIANCE", "RELIANCE", "Reliance Industries (demo)", "NSE", "equity", True, 0.10, 1),
]
BARS_PER_SESSION = 75   # 09:15 .. 15:25 start times, 5-minute bars


def _session_path(rng, n_sessions, start_price, daily_vol):
    """Regime-switching daily drift so the demo shows trends and ranges."""
    drift = np.zeros(n_sessions)
    i = 0
    while i < n_sessions:
        length = rng.integers(15, 60)
        drift[i:i + length] = rng.choice([-0.0012, 0.0, 0.0, 0.0015])
        i += length
    vol = daily_vol * np.exp(rng.normal(0, 0.35, n_sessions))
    return drift, vol


def _intraday(rng, sessions, start_price, daily_vol, volume_base):
    drift, vol = _session_path(rng, len(sessions), start_price, daily_vol)
    rows, idx = [], []
    price = start_price
    for s, d, v in zip(sessions, drift, vol):
        gap = rng.normal(0, v * 0.35)
        price *= np.exp(gap)
        steps = rng.normal(d / BARS_PER_SESSION, v / np.sqrt(BARS_PER_SESSION), BARS_PER_SESSION)
        closes = price * np.exp(np.cumsum(steps))
        opens = np.concatenate([[price], closes[:-1]])
        wick = np.abs(rng.normal(0, v / np.sqrt(BARS_PER_SESSION) * 0.6, (2, BARS_PER_SESSION)))
        highs = np.maximum(opens, closes) * (1 + wick[0])
        lows = np.minimum(opens, closes) * (1 - wick[1])
        u = np.linspace(-1, 1, BARS_PER_SESSION)
        shape = 1 + 1.6 * u ** 2                     # U-shaped intraday volume
        vols = (volume_base * shape * rng.lognormal(0, 0.4, BARS_PER_SESSION)).round() if volume_base else np.zeros(BARS_PER_SESSION)
        start = pd.Timestamp(s, tz="Asia/Kolkata") + pd.Timedelta(hours=9, minutes=15)
        idx.extend(start + pd.Timedelta(minutes=5 * k) for k in range(BARS_PER_SESSION))
        rows.append(np.column_stack([opens, highs, lows, closes, vols]))
        price = closes[-1]
    df = pd.DataFrame(np.vstack(rows), columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex(idx))
    tick = 0.05
    for col in ("open", "high", "low", "close"):
        df[col] = ((df[col] / tick).round() * tick).round(2)
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    return df


class FixtureMarketDataProvider:
    name = "fixture-demo"
    data_mode = DataMode.SIMULATED

    def __init__(self, last_session: date = date(2026, 11, 10), n_sessions: int = 520, seed: int = 20261111):
        self.calendar = demo_calendar()
        start = last_session - timedelta(days=int(n_sessions * 1.5))
        sessions = self.calendar.sessions(start, last_session)[-n_sessions:]
        self.sessions = sessions
        rng = np.random.default_rng(seed)
        nifty = _intraday(rng, sessions, 23500.0, 0.009, 0)
        bank = _intraday(rng, sessions, 51000.0, 0.011, 0)
        rel = _intraday(rng, sessions, 1300.0, 0.014, 60_000)
        fut = nifty.copy()
        basis = 1.0012
        for c in ("open", "high", "low", "close"):
            fut[c] = ((fut[c] * basis / 0.1).round() * 0.1).round(2)
        fut["volume"] = (900_000 * (1 + 1.6 * np.tile(np.linspace(-1, 1, BARS_PER_SESSION) ** 2, len(sessions)))
                         * rng.lognormal(0, 0.4, len(fut))).round()
        self._intraday = {"NIFTY50": nifty, "BANKNIFTY": bank, "RELIANCE": rel, "NIFTYFUT": fut}
        self._daily = {k: daily_from_intraday(v) for k, v in self._intraday.items()}
        self.generated_at = datetime.now(timezone.utc)

    def get_instruments(self):
        return list(INSTRUMENTS)

    def instrument(self, instrument_id: str) -> Instrument:
        for i in INSTRUMENTS:
            if i.id == instrument_id:
                return i
        raise KeyError(instrument_id)

    def get_historical_candles(self, instrument_id, interval="day", start=None, end=None):
        self.instrument(instrument_id)
        if interval == "day":
            df = self._daily[instrument_id]
        elif interval == "5minute":
            df = self._intraday[instrument_id]
        elif interval in ("15minute", "60minute"):
            df = resample_intraday(self._intraday[instrument_id], int(interval.replace("minute", "")))
            df = df.drop(columns="bars")
        else:
            raise ValueError(f"unsupported interval {interval}")
        if start or end:
            keys = df.index if interval == "day" else df.index.tz_convert("Asia/Kolkata").date
            mask = np.ones(len(df), bool)
            if start:
                mask &= np.array([k >= start for k in keys])
            if end:
                mask &= np.array([k <= end for k in keys])
            df = df[mask]
        return df

    def last_update(self, instrument_id):
        last = self._intraday[instrument_id].index[-1] + pd.Timedelta(minutes=5)
        return last.to_pydatetime()


# ======================================================================
# service
# ======================================================================
# Application service: turns provider data into analytics responses.
# Every response carries provenance so the UI can label DEMO / live / stale.
def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (date, datetime, pd.Timestamp)):
        return x.isoformat()
    return x


class Service:
    def __init__(self, provider):
        self.p = provider
        self.cal = provider.calendar

    # ---------- helpers ----------
    def next_session(self) -> date:
        d = self.p.sessions[-1] + timedelta(days=1)
        while not self.cal.is_session(d):
            d += timedelta(days=1)
        return d

    def _as_of(self, as_of: date | None) -> date:
        return as_of or self.next_session()

    def _daily_before(self, iid, as_of):
        d = self.p.get_historical_candles(iid, "day")
        return d[d.index < as_of]

    def provenance(self, iid, interval, df) -> dict:
        issues = validate_candles(df, has_volume=self.p.instrument(iid).has_volume)
        status = "error" if any(i.severity == "error" for i in issues) else "passed"
        return Provenance(
            provider=self.p.name, data_mode=self.p.data_mode, interval=interval,
            first_bar=str(df.index[0]), last_bar=str(df.index[-1]),
            ingested_at=self.p.generated_at.isoformat(),
            calendar_source=self.cal.source, validation=status,
        ).to_dict()

    # ---------- levels ----------
    def _width_history(self, daily: pd.DataFrame, period: str) -> pd.Series:
        if period == "day":
            bars = daily
        else:
            bars = aggregate(daily, period).iloc[:-1]   # drop the last (may be partial)
        vals = [cpr(r.high, r.low, r.close).width_pct for r in bars.itertuples()]
        return pd.Series(vals)

    def cpr(self, iid, period="day", as_of=None) -> dict:
        as_of = self._as_of(as_of)
        daily = self.p.get_historical_candles(iid, "day")
        src = previous_completed(daily, as_of, period, self.cal)
        lvl = cpr(src.high, src.low, src.close)
        # previous period's CPR for relation analysis
        prev_src = previous_completed(daily, src.start, period, self.cal)
        prev = cpr(prev_src.high, prev_src.low, prev_src.close)
        hist_daily = daily[daily.index < src.start]
        width = classify_width(self._width_history(hist_daily, period), lvl.width_pct,
                               lookback={"day": 250, "week": 104, "month": 36}[period],
                               min_samples={"day": 60, "week": 26, "month": 12}[period])
        before = daily[daily.index < as_of]
        ref = float(before["close"].iloc[-1])
        return _clean({
            "instrument": iid, "period": period, "applies_to": as_of, "kind": "fixed",
            "source_ohlc": src.to_dict(), "levels": lvl.to_dict(),
            "previous": {"source_end": prev_src.end, "top": prev.top, "bottom": prev.bottom, "pivot": prev.pivot},
            "relation_to_previous": cpr_relation(prev, lvl).value,
            "width_classification": width,
            "reference_price": {"value": ref, "label": f"last close {before.index[-1].isoformat()}",
                                "position": price_position(ref, lvl)},
            "provenance": self.provenance(iid, "day", before),
        })

    def pivots(self, iid, period="day", as_of=None, camarilla_multiplier=1.1) -> dict:
        as_of = self._as_of(as_of)
        daily = self.p.get_historical_candles(iid, "day")
        src = previous_completed(daily, as_of, period, self.cal)
        return _clean({
            "instrument": iid, "period": period, "applies_to": as_of,
            "source_ohlc": src.to_dict(),
            "floor": floor_pivots(src.high, src.low, src.close).to_dict(),
            "camarilla": camarilla(src.high, src.low, src.close, camarilla_multiplier).to_dict(),
            "provenance": self.provenance(iid, "day", daily[daily.index < as_of]),
        })

    def cpr_history(self, iid, sessions=40, as_of=None) -> dict:
        as_of = self._as_of(as_of)
        daily = self._daily_before(iid, as_of)
        h = cpr_history(daily).iloc[-sessions:]
        candles = daily.loc[h.index]
        rows = []
        for (d, r), c in zip(h.iterrows(), candles.itertuples()):
            rows.append({"session": d, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                         "cpr_top": r.top, "cpr_bottom": r.bottom, "pivot": r.pivot,
                         "width_pct": r.width_pct, "relation": r.relation, "virgin": bool(r.virgin),
                         "open_vs_cpr": r.open_vs_cpr})
        virgin_rate = float(h["virgin"].mean()) if len(h) else None
        return _clean({"instrument": iid, "sessions": rows, "virgin_share": virgin_rate,
                       "provenance": self.provenance(iid, "day", daily)})

    # ---------- indicators ----------
    def indicators(self, iid, interval="day", as_of=None) -> dict:
        as_of = self._as_of(as_of)
        df = self.p.get_historical_candles(iid, interval)
        keys = df.index if interval == "day" else df.index.tz_convert("Asia/Kolkata").date
        df = df[np.array([k < as_of for k in keys])]
        inst = self.p.instrument(iid)
        h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
        dmi = ind.adx_dmi(h, l, c)
        m = ind.macd(c)
        bb = ind.bollinger(c)
        last = lambda s: s.iloc[-1]
        out = {
            "trend": {"ema20": last(ind.ema(c, 20)), "ema50": last(ind.ema(c, 50)),
                      "ema200": last(ind.ema(c, 200)),
                      "adx14": last(dmi.adx), "plus_di14": last(dmi.plus_di), "minus_di14": last(dmi.minus_di)},
            "momentum": {"rsi14": last(ind.rsi(c)), "macd": last(m.macd),
                         "macd_signal": last(m.signal), "macd_histogram": last(m.histogram)},
            "volatility": {"atr14": last(ind.atr(h, l, c)),
                           "atr14_pct": last(ind.atr(h, l, c)) / last(c) * 100,
                           "bb_upper": last(bb.upper), "bb_middle": last(bb.middle),
                           "bb_lower": last(bb.lower), "bb_bandwidth_pct": last(bb.bandwidth_pct)},
        }
        if inst.has_volume:
            out["volume"] = {"relative_volume20": last(ind.relative_volume(v)),
                             "obv": last(ind.obv(c, v)), "mfi14": last(ind.mfi(h, l, c, v))}
        else:
            out["volume"] = {"status": "unavailable",
                             "reason": "index has no traded volume; volume indicators use the future"
                                       + (f" ({inst.volume_proxy})" if inst.volume_proxy else " when configured")}
        return _clean({"instrument": iid, "interval": interval, "as_of_bar": df.index[-1],
                       "close": last(c), "indicators": out,
                       "regime": regime(df) if interval == "day" else None,
                       "provenance": self.provenance(iid, interval, df)})

    def session_levels(self, iid, as_of=None) -> dict:
        """Previous-session intraday facts: VWAP, opening range, high/low."""
        as_of = self._as_of(as_of)
        last_session = self.cal.previous_session(as_of)
        inst = self.p.instrument(iid)
        intra = self.p.get_historical_candles(iid, "5minute", last_session, last_session)
        res = {"instrument": iid, "session": last_session,
               "high": float(intra["high"].max()), "low": float(intra["low"].min()),
               "close": float(intra["close"].iloc[-1]),
               "opening_range_15m": opening_range(intra, last_session, 15)}
        vsrc = iid if inst.has_volume else inst.volume_proxy
        if vsrc:
            vi = self.p.get_historical_candles(vsrc, "5minute", last_session, last_session)
            res["vwap"] = {"value": float(ind.vwap(vi).iloc[-1]), "source_instrument": vsrc,
                           "note": None if vsrc == iid else "computed on the future's prices and volume"}
        else:
            res["vwap"] = {"value": None, "status": "unavailable", "reason": "no volume source configured"}
        return _clean(res)

    def multi_timeframe(self, iid, as_of=None) -> dict:
        as_of = self._as_of(as_of)
        inst = self.p.instrument(iid)
        daily = self._daily_before(iid, as_of)
        frames = []
        for label, interval in (("15m", "15minute"), ("1h", "60minute")):
            df = self.p.get_historical_candles(iid, interval)
            df = df[np.array([k < as_of for k in df.index.tz_convert("Asia/Kolkata").date])]
            frames.append(timeframe_summary(df, label, inst.has_volume))
        frames.append(timeframe_summary(daily, "day", inst.has_volume))
        cw, cm = week_key(as_of), month_key(as_of)
        wk = aggregate(daily, "week")
        wk = wk[[week_key(e) < cw for e in wk["end"]]]
        mo = aggregate(daily, "month")
        mo = mo[[month_key(e) < cm for e in mo["end"]]]
        frames.append(timeframe_summary(wk, "week", inst.has_volume))
        frames.append(timeframe_summary(mo, "month", inst.has_volume))
        trends = [f.get("trend") for f in frames if f.get("status") == "ok"]
        ups, downs = trends.count("up"), trends.count("down")
        align = "aligned_up" if ups == len(trends) else "aligned_down" if downs == len(trends) \
            else "mostly_up" if ups > downs else "mostly_down" if downs > ups else "conflicted"
        return _clean({"instrument": iid, "applies_to": as_of, "timeframes": frames,
                       "alignment": align,
                       "note": "Only completed bars are used; the current week and month are excluded."})

    # ---------- risk ----------
    def size(self, capital, entry, stop, target=None, lot_size=1, direction="long", risk_pct=1.0):
        pol = RiskPolicy(capital=capital, risk_per_trade_pct=risk_pct)
        s = position_size(pol, entry, stop, lot_size, direction)
        out = {"ok": s.ok, "quantity": s.quantity, "lots": s.lots, "rupee_risk": s.rupee_risk,
               "risk_budget": pol.risk_budget, "reason": s.reason}
        if target is not None and s.ok:
            rr = reward_risk(entry, stop, target, direction)
            out["reward_risk"] = rr
            out["meets_minimum_rr"] = rr >= pol.min_reward_risk
            if rr < pol.min_reward_risk:
                out["ok"] = False
                out["reason"] = f"reward/risk {rr:.2f} is below the 1:{pol.min_reward_risk:g} minimum"
        return _clean(out)

    # ---------- health ----------
    def data_health(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        rows = []
        for inst in self.p.get_instruments():
            daily = self.p.get_historical_candles(inst.id, "day")
            intra = self.p.get_historical_candles(inst.id, "5minute")
            issues = validate_candles(intra, has_volume=inst.has_volume)
            miss = missing_sessions(daily.index, self.cal)
            fr = freshness(self.p.last_update(inst.id), now, timedelta(minutes=5))
            rows.append({"instrument": inst.id, "issues": [i.to_dict() for i in issues],
                         "missing_sessions": miss, "freshness": fr,
                         "live_recommendations_allowed": False,
                         "why_blocked": "simulated data" if self.p.data_mode.value == "simulated" else
                                        "historical data, not a live feed" if self.p.data_mode.value == "historical" else
                                        ("stale feed" if fr["stale"] else None)})
        return _clean({"provider": self.p.name, "data_mode": self.p.data_mode.value,
                       "demo": self.p.data_mode.value == "simulated", "checked_at": now, "instruments": rows})


# ======================================================================
# main
# ======================================================================
# Vriddhi API. Read-only analytics. There are NO order routes, by design.
app = FastAPI(title="Vriddhi", version="0.1.0",
              description="Analysis only. AI recommends, the human executes. No order placement.")


def make_provider(kind: str | None = None):
    """PRAVIN_DATA_PROVIDER=fixture (default, simulated) or kite (local Zerodha history)."""
    import os
    kind = kind or os.environ.get("PRAVIN_DATA_PROVIDER", "fixture")
    if kind == "kite":
        from pravin_kite import StoreProvider
        return StoreProvider()
    if kind == "fixture":
        return FixtureMarketDataProvider()
    raise ValueError(f"unknown data provider {kind!r}")


@lru_cache(maxsize=1)
def svc() -> Service:
    return Service(make_provider())


@app.exception_handler(QuantError)
def quant_error(_, exc: QuantError):
    return JSONResponse(status_code=422, content={
        "status": "no_data", "error": type(exc).__name__, "detail": str(exc),
        "guidance": "Calculation refused rather than guessed. Treat as NO TRADE."})


def _inst(iid: str):
    try:
        return svc().p.instrument(iid)
    except KeyError:
        raise HTTPException(404, f"unknown instrument {iid}") from None


Period = Literal["day", "week", "month"]


@app.get("/health")
def health():
    return {"status": "ok", "order_placement": "disabled"}


@app.get("/api/v1/market/status")
def market_status():
    s = svc()
    return _clean({"provider": s.p.name, "data_mode": s.p.data_mode.value, "demo": s.p.data_mode.value == "simulated",
                   "last_session_in_data": s.p.sessions[-1], "next_session": s.next_session(),
                   "calendar": s.cal.source})


@app.get("/api/v1/instruments")
def instruments():
    return [i.to_dict() for i in svc().p.get_instruments()]


@app.get("/api/v1/instruments/{iid}")
def instrument(iid: str):
    return _inst(iid).to_dict()


@app.get("/api/v1/market/candles/{iid}")
def candles(iid: str, interval: Literal["day", "5minute", "15minute", "60minute"] = "day",
            limit: int = Query(100, ge=1, le=5000)):
    _inst(iid)
    df = svc().p.get_historical_candles(iid, interval).tail(limit)
    return _clean({"instrument": iid, "interval": interval,
                   "candles": [{"t": str(t), **r} for t, r in zip(df.index, df.to_dict("records"))],
                   "provenance": svc().provenance(iid, interval, df)})


@app.get("/api/v1/analytics/cpr/{iid}")
def get_cpr(iid: str, period: Period = "day", as_of: date | None = None):
    _inst(iid)
    return svc().cpr(iid, period, as_of)


@app.get("/api/v1/analytics/pivots/{iid}")
def get_pivots(iid: str, period: Period = "day", as_of: date | None = None,
               camarilla_multiplier: float = Query(1.1, gt=0, le=3)):
    _inst(iid)
    return svc().pivots(iid, period, as_of, camarilla_multiplier)


@app.get("/api/v1/analytics/cpr-history/{iid}")
def get_cpr_history(iid: str, sessions: int = Query(40, ge=5, le=250), as_of: date | None = None):
    _inst(iid)
    return svc().cpr_history(iid, sessions, as_of)


@app.get("/api/v1/analytics/indicators/{iid}")
def get_indicators(iid: str, interval: Literal["day", "15minute", "60minute"] = "day",
                   as_of: date | None = None):
    _inst(iid)
    return svc().indicators(iid, interval, as_of)


@app.get("/api/v1/analytics/session-levels/{iid}")
def get_session_levels(iid: str, as_of: date | None = None):
    _inst(iid)
    return svc().session_levels(iid, as_of)


@app.get("/api/v1/analytics/multi-timeframe/{iid}")
def get_mtf(iid: str, as_of: date | None = None):
    _inst(iid)
    return svc().multi_timeframe(iid, as_of)


@app.get("/api/v1/risk/position-size")
def get_size(capital: float = Query(..., gt=0), entry: float = Query(..., gt=0),
             stop: float = Query(..., gt=0), target: float | None = None,
             lot_size: int = Query(1, ge=1), direction: Literal["long", "short"] = "long",
             risk_pct: float = Query(1.0, gt=0, le=5)):
    return svc().size(capital, entry, stop, target, lot_size, direction, risk_pct)


@app.get("/api/v1/data-health")
def data_health():
    return svc().data_health()
