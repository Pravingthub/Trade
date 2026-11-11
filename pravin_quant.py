"""Vriddhi quantitative engine (single module).

Deterministic calculations: CPR, pivots, period aggregation, indicators,
data-quality checks and risk controls. No network, no AI, no randomness.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from math import floor
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ======================================================================
# errors
# ======================================================================

class QuantError(Exception):
    """Base class for engine errors. Callers should treat these as NO TRADE."""


class InvalidOHLC(QuantError):
    """Input candle violates OHLC invariants."""


class InsufficientData(QuantError):
    """Not enough completed history to calculate the value honestly."""


class IncompletePeriod(QuantError):
    """A period is missing sessions the exchange calendar says should exist."""


# ======================================================================
# calendar
# ======================================================================
# Exchange session calendar.
#
# Weekly/monthly CPR must follow real exchange sessions, not 7-day windows, so the
# calendar is the single source of truth for which dates are trading sessions.
#
# Holidays are NOT hardcoded for production. Load the official NSE holiday list
# (published each year by NSE circular) into `holidays`. Special sessions such as
# Muhurat trading on a weekend go into `extra_sessions`.
IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


@dataclass(frozen=True)
class ExchangeCalendar:
    holidays: frozenset[date] = field(default_factory=frozenset)
    extra_sessions: frozenset[date] = field(default_factory=frozenset)
    source: str = "unspecified"

    def is_session(self, d: date) -> bool:
        if d in self.extra_sessions:
            return True
        return d.weekday() < 5 and d not in self.holidays

    def sessions(self, start: date, end: date) -> list[date]:
        """All sessions in [start, end] inclusive."""
        out, d = [], start
        while d <= end:
            if self.is_session(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def previous_session(self, d: date) -> date:
        p = d - timedelta(days=1)
        for _ in range(30):
            if self.is_session(p):
                return p
            p -= timedelta(days=1)
        raise ValueError(f"No session found in the 30 days before {d}")


def week_key(d: date) -> tuple[int, int]:
    """Exchange week identity: ISO year + ISO week (Monday-start)."""
    iso = d.isocalendar()
    return (iso.year, iso.week)


def month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


# ======================================================================
# levels
# ======================================================================
# CPR, traditional floor pivots and Camarilla pivots.
#
# All functions take the previous COMPLETED period's high, low and close.
# Values are returned unrounded; rounding to tick size is a display concern.
def validate_hlc(high: float, low: float, close: float) -> None:
    for name, v in (("high", high), ("low", low), ("close", close)):
        if v is None or v != v or v <= 0:  # None, NaN, non-positive
            raise InvalidOHLC(f"{name} must be a positive number, got {v!r}")
    if high < low:
        raise InvalidOHLC(f"high {high} is below low {low}")
    if not (low <= close <= high):
        raise InvalidOHLC(f"close {close} is outside the range [{low}, {high}]")


@dataclass(frozen=True)
class CPR:
    pivot: float
    bc: float          # raw (H+L)/2
    tc: float          # raw 2P - BC
    top: float         # max(TC, BC)
    bottom: float      # min(TC, BC)
    width: float
    width_pct: float   # width / pivot * 100

    def to_dict(self) -> dict:
        return asdict(self)


def cpr(high: float, low: float, close: float) -> CPR:
    validate_hlc(high, low, close)
    p = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2.0 * p - bc
    top, bottom = max(tc, bc), min(tc, bc)
    width = top - bottom
    return CPR(pivot=p, bc=bc, tc=tc, top=top, bottom=bottom,
               width=width, width_pct=width / p * 100.0)


@dataclass(frozen=True)
class FloorPivots:
    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float

    def to_dict(self) -> dict:
        return asdict(self)


def floor_pivots(high: float, low: float, close: float) -> FloorPivots:
    validate_hlc(high, low, close)
    p = (high + low + close) / 3.0
    rng = high - low
    return FloorPivots(
        pivot=p,
        r1=2 * p - low, s1=2 * p - high,
        r2=p + rng, s2=p - rng,
        r3=high + 2 * (p - low), s3=low - 2 * (high - p),
    )


@dataclass(frozen=True)
class Camarilla:
    multiplier: float
    r1: float
    r2: float
    r3: float
    r4: float
    s1: float
    s2: float
    s3: float
    s4: float

    def to_dict(self) -> dict:
        return asdict(self)


def camarilla(high: float, low: float, close: float, multiplier: float = 1.1) -> Camarilla:
    """Classic Camarilla convention: level_n = C +/- Range * m / d_n,
    with divisors 12, 6, 4, 2 for levels 1..4. Multiplier is configurable."""
    validate_hlc(high, low, close)
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")
    rng = high - low
    k = rng * multiplier
    return Camarilla(
        multiplier=multiplier,
        r1=close + k / 12, r2=close + k / 6, r3=close + k / 4, r4=close + k / 2,
        s1=close - k / 12, s2=close - k / 6, s3=close - k / 4, s4=close - k / 2,
    )


class CPRRelation(str, Enum):
    """How today's CPR sits relative to the previous period's CPR."""
    HIGHER_VALUE = "higher_value"                  # entirely above previous
    LOWER_VALUE = "lower_value"                    # entirely below previous
    OVERLAPPING_HIGHER = "overlapping_higher"
    OVERLAPPING_LOWER = "overlapping_lower"
    INSIDE = "inside"                              # contained in previous
    OUTSIDE = "outside"                            # engulfs previous
    UNCHANGED = "unchanged"


def cpr_relation(prev: CPR, curr: CPR, tol: float = 1e-9) -> CPRRelation:
    if abs(curr.top - prev.top) <= tol and abs(curr.bottom - prev.bottom) <= tol:
        return CPRRelation.UNCHANGED
    if curr.bottom > prev.top + tol:
        return CPRRelation.HIGHER_VALUE
    if curr.top < prev.bottom - tol:
        return CPRRelation.LOWER_VALUE
    if curr.top <= prev.top + tol and curr.bottom >= prev.bottom - tol:
        return CPRRelation.INSIDE
    if curr.top >= prev.top - tol and curr.bottom <= prev.bottom + tol:
        return CPRRelation.OUTSIDE
    return CPRRelation.OVERLAPPING_HIGHER if curr.pivot > prev.pivot else CPRRelation.OVERLAPPING_LOWER


def is_virgin(level: CPR, session_high: float, session_low: float) -> bool:
    """A CPR is 'virgin' if the session it applied to never traded into it."""
    return session_low > level.top or session_high < level.bottom


def price_position(price: float, level: CPR) -> str:
    if price > level.top:
        return "above"
    if price < level.bottom:
        return "below"
    return "inside"


# ======================================================================
# periods
# ======================================================================
# Period aggregation and 'previous completed period' selection.
#
# Daily candles are the input. Weekly and monthly bars are built from the
# sessions that actually traded, grouped by exchange week / calendar month.
# A period is only used if it is complete (every calendar session present and
# the period has ended before `as_of`). This is what prevents look-ahead and
# silent use of partial data.
Period = Literal["day", "week", "month"]
OHLCV = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class SourceOHLC:
    """The exact source bar used for a set of levels. Always shown to the user."""
    period: str
    start: date
    end: date
    sessions: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d


def _as_dates(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()
    df.index = pd.Index([pd.Timestamp(x).date() for x in df.index], name="session")
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()
    return df


def _key(period: Period):
    if period == "week":
        return week_key
    if period == "month":
        return month_key
    return lambda d: d


def aggregate(daily: pd.DataFrame, period: Literal["week", "month"]) -> pd.DataFrame:
    """Aggregate daily candles into exchange weeks or months.
    Returns one row per period with start/end session and session count."""
    df = _as_dates(daily)
    keyf = _key(period)
    keys = pd.Series(["%d-%02d" % keyf(d) for d in df.index], index=df.index)
    rows = []
    for k, g in df.groupby(keys, sort=True):
        rows.append({
            "key": k, "start": g.index[0], "end": g.index[-1], "sessions": len(g),
            "open": g["open"].iloc[0], "high": g["high"].max(), "low": g["low"].min(),
            "close": g["close"].iloc[-1], "volume": g["volume"].sum(),
        })
    return pd.DataFrame(rows).set_index("end", drop=False)


def previous_completed(daily: pd.DataFrame, as_of: date, period: Period,
                       calendar: ExchangeCalendar) -> SourceOHLC:
    """Source bar for levels that apply on trading date `as_of`.

    day   -> last session strictly before as_of
    week  -> last exchange week that ended before as_of's week
    month -> last month that ended before as_of's month
    Only data strictly before `as_of` is ever read.
    """
    df = _as_dates(daily)
    hist = df[df.index < as_of]
    if hist.empty:
        raise InsufficientData(f"No completed sessions before {as_of}")

    if period == "day":
        expected = calendar.previous_session(as_of)
        last = hist.index[-1]
        if last != expected:
            raise IncompletePeriod(
                f"Expected previous session {expected} but latest data is {last}")
        r = hist.iloc[-1]
        return SourceOHLC("day", last, last, 1, float(r.open), float(r.high),
                          float(r.low), float(r.close), float(r.volume))

    keyf = _key(period)
    bounds = week_bounds if period == "week" else month_bounds
    current = keyf(as_of)
    prior = hist[[keyf(d) < current for d in hist.index]]
    if prior.empty:
        raise InsufficientData(f"No completed {period} before {as_of}")
    target = keyf(prior.index[-1])
    g = prior[[keyf(d) == target for d in prior.index]]
    lo, hi = bounds(g.index[0])
    expected = calendar.sessions(lo, hi)
    missing = sorted(set(expected) - set(g.index))
    if missing:
        raise IncompletePeriod(
            f"{period} {target} is missing sessions: {', '.join(d.isoformat() for d in missing)}")
    return SourceOHLC(period, g.index[0], g.index[-1], len(g), float(g["open"].iloc[0]),
                      float(g["high"].max()), float(g["low"].min()),
                      float(g["close"].iloc[-1]), float(g["volume"].sum()))


def resample_intraday(intraday: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample intraday candles (tz-aware index) into larger bars that never
    cross a session boundary. Bars are anchored to the 09:15 IST open."""
    df = intraday.copy()
    idx = df.index.tz_convert("Asia/Kolkata")
    session_start = idx.normalize() + pd.Timedelta(hours=9, minutes=15)
    bucket = ((idx - session_start) // pd.Timedelta(minutes=minutes)).astype(int)
    keys = session_start + bucket * pd.Timedelta(minutes=minutes)
    g = df.groupby(keys)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
        "bars": g["close"].count(),
    })
    return out


def daily_from_intraday(intraday: pd.DataFrame) -> pd.DataFrame:
    idx = intraday.index.tz_convert("Asia/Kolkata")
    g = intraday.groupby(idx.date)
    return pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
    })


def opening_range(intraday: pd.DataFrame, session: date, minutes: int = 15) -> dict | None:
    """High/low of the first `minutes` of a session. None if that window is not
    fully present (a partial opening range is never reported as complete)."""
    idx = intraday.index.tz_convert("Asia/Kolkata")
    day = intraday[idx.date == session]
    if day.empty:
        return None
    didx = day.index.tz_convert("Asia/Kolkata")
    start = pd.Timestamp(session, tz="Asia/Kolkata") + pd.Timedelta(hours=9, minutes=15)
    window = day[(didx >= start) & (didx < start + pd.Timedelta(minutes=minutes))]
    if window.empty:
        return None
    bar = (didx[1] - didx[0]) if len(didx) > 1 else pd.Timedelta(minutes=minutes)
    if len(window) < int(pd.Timedelta(minutes=minutes) / bar):
        return None
    return {"minutes": minutes, "high": float(window["high"].max()),
            "low": float(window["low"].min())}


# ======================================================================
# indicators
# ======================================================================
# Technical indicators.
#
# Convention: Wilder smoothing (RMA) and EMA are seeded with a simple average of
# the first `n` values, which is the convention used by TA-Lib and by the
# TradingView-style charts on Kite. Values before the warm-up are NaN; nothing
# is extrapolated. Every function takes and returns pandas objects aligned to the
# input index.
#
# Formula notes live next to each function. Parity with TA-Lib is checked in
# tests/unit/test_indicator_parity.py.
NaN = np.nan


def _arr(s: pd.Series) -> np.ndarray:
    return s.to_numpy(dtype=float)


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _seeded_smooth(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """Seed with SMA of first n valid values, then out = prev + alpha*(x-prev)."""
    out = np.full(len(x), NaN)
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < n:
        return out
    start = valid[0]
    if np.isnan(x[start:start + n]).any():
        raise ValueError("gap inside indicator warm-up window")
    seed_i = start + n - 1
    out[seed_i] = x[start:start + n].mean()
    for i in range(seed_i + 1, len(x)):
        out[i] = out[i - 1] + alpha * (x[i] - out[i - 1])
    return out


def ema(s: pd.Series, n: int) -> pd.Series:
    """EMA, alpha = 2/(n+1), SMA seed. Warm-up: n bars."""
    return pd.Series(_seeded_smooth(_arr(s), n, 2.0 / (n + 1)), index=s.index)


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average, alpha = 1/n, SMA seed."""
    return pd.Series(_seeded_smooth(_arr(s), n, 1.0 / n), index=s.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI. First value at bar n (needs n price changes)."""
    d = close.diff()
    gain, loss = d.clip(lower=0), (-d).clip(lower=0)
    ag, al = rma(gain, n), rma(loss, n)
    out = 100 - 100 / (1 + ag / al)
    out[(al == 0) & (ag > 0)] = 100.0
    out[(al == 0) & (ag == 0)] = 50.0
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """TR = max(H-L, |H-prevC|, |L-prevC|). First bar is NaN (no previous close)."""
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = NaN
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder ATR. First value at bar n."""
    return rma(true_range(high, low, close), n)


def adx_dmi(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    """Wilder +DI, -DI and ADX (TA-Lib algorithm).

    +DM = up-move if up-move > down-move and > 0, else 0 (and vice versa).
    TR, +DM, -DM use Wilder running sums seeded with the sum of the first n-1
    values; DI = 100 * DMsum / TRsum; DX = 100*|+DI - -DI| / (+DI + -DI);
    ADX = Wilder average of DX seeded with the mean of the first n DX values.
    First DI at bar n, first ADX at bar 2n-1.
    """
    h, l, c = _arr(high), _arr(low), _arr(close)
    m = len(h)
    pdi = np.full(m, NaN); mdi = np.full(m, NaN); adx = np.full(m, NaN)
    if m < 2 * n:
        return pd.DataFrame({"plus_di": pdi, "minus_di": mdi, "adx": adx}, index=high.index)
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    # arrays above are aligned to bars 1..m-1
    s_tr = tr[:n - 1].sum(); s_p = pdm[:n - 1].sum(); s_m = mdm[:n - 1].sum()
    dx = np.full(m, NaN)
    for j in range(n - 1, m - 1):          # j indexes the diff arrays; bar = j+1
        s_tr = s_tr - s_tr / n + tr[j]
        s_p = s_p - s_p / n + pdm[j]
        s_m = s_m - s_m / n + mdm[j]
        bar = j + 1
        if s_tr > 0:
            pdi[bar] = 100 * s_p / s_tr
            mdi[bar] = 100 * s_m / s_tr
            tot = pdi[bar] + mdi[bar]
            dx[bar] = 0.0 if tot == 0 else 100 * abs(pdi[bar] - mdi[bar]) / tot
    first = 2 * n - 1
    adx[first] = np.nanmean(dx[n:first + 1])
    for i in range(first + 1, m):
        adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return pd.DataFrame({"plus_di": pdi, "minus_di": mdi, "adx": adx}, index=high.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD = EMA(fast) - EMA(slow), each independently SMA-seeded from bar 0
    (chart-platform convention). Signal = EMA(signal) of MACD. TA-Lib aligns the
    fast EMA's seed to the slow one, so the two agree only after warm-up; the
    difference decays geometrically and is checked in tests."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "histogram": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Middle = SMA(n); bands = middle +/- k * population std-dev (ddof=0)."""
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"middle": mid, "upper": mid + k * sd, "lower": mid - k * sd,
                         "bandwidth_pct": (2 * k * sd) / mid * 100})


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume, starting at the first bar's volume (TA-Lib convention)."""
    direction = np.sign(close.diff()).fillna(0)
    out = (direction * volume).cumsum() + volume.iloc[0]
    return out


def mfi(high, low, close, volume, n: int = 14) -> pd.Series:
    """Money Flow Index. Typical price (H+L+C)/3; flows split by direction of
    typical price change; first value at bar n."""
    tp = (high + low + close) / 3
    rmf = tp * volume
    diff = tp.diff()
    pos = rmf.where(diff > 0, 0.0)
    neg = rmf.where(diff < 0, 0.0)
    pos.iloc[0] = NaN; neg.iloc[0] = NaN
    ps = pos.rolling(n, min_periods=n).sum()
    ns = neg.rolling(n, min_periods=n).sum()
    out = 100 * ps / (ps + ns)
    out[(ps + ns) == 0] = NaN
    return out


def vwap(intraday: pd.DataFrame) -> pd.Series:
    """Session VWAP, reset at each IST session. Uses typical price (H+L+C)/3.

    Returns NaN for any session where volume is missing or all zero, because an
    index such as NIFTY 50 has no traded volume; use the futures contract's
    volume for an index VWAP instead of fabricating one."""
    idx = intraday.index.tz_convert("Asia/Kolkata")
    session = pd.Series(idx.date, index=intraday.index)
    tp = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    vol = intraday["volume"].astype(float)
    pv = (tp * vol).groupby(session).cumsum()
    cv = vol.groupby(session).cumsum()
    out = pv / cv
    out[cv <= 0] = NaN
    return out


def anchored_vwap(intraday: pd.DataFrame, anchor: pd.Timestamp) -> pd.Series:
    sub = intraday[intraday.index >= anchor]
    tp = (sub["high"] + sub["low"] + sub["close"]) / 3
    cv = sub["volume"].cumsum()
    out = (tp * sub["volume"]).cumsum() / cv
    out[cv <= 0] = NaN
    return out.reindex(intraday.index)


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    """Volume divided by the average of the PREVIOUS n bars (excludes current)."""
    return volume / volume.shift(1).rolling(n, min_periods=n).mean()


def percentile_rank(history: pd.Series, value: float) -> float:
    """Share of historical values <= value, 0..100."""
    h = history.dropna()
    if h.empty:
        return NaN
    return float((h <= value).mean() * 100)


# ======================================================================
# risk
# ======================================================================
# Risk controls. These encode the user's stated failure modes:
# overtrading/revenge trading, ignored stops, and oversized option risk.
#
# Nothing here can place or block an order in the broker. It decides whether
# Vriddhi is allowed to *recommend* another trade, and how big it may be.
Direction = Literal["long", "short"]


@dataclass(frozen=True)
class RiskPolicy:
    capital: float
    risk_per_trade_pct: float = 1.0
    daily_loss_stop_pct: float = 2.0
    weekly_loss_stop_pct: float = 5.0
    max_trades_per_day: int = 3
    min_reward_risk: float = 2.0
    cooldown_after_loss_min: int = 30
    consecutive_loss_lockout: int = 2   # two losses in a row ends the day

    def __post_init__(self):
        if self.capital <= 0:
            raise ValueError("capital must be positive")
        if not (0 < self.risk_per_trade_pct <= 5):
            raise ValueError("risk_per_trade_pct must be in (0, 5]")

    @property
    def risk_budget(self) -> float:
        return self.capital * self.risk_per_trade_pct / 100


@dataclass(frozen=True)
class SizeResult:
    ok: bool
    quantity: int
    lots: int
    rupee_risk: float
    risk_per_unit: float
    reason: str


def position_size(policy: RiskPolicy, entry: float, stop: float, lot_size: int = 1,
                  direction: Direction = "long") -> SizeResult:
    """Largest whole-lot quantity whose loss at the stop fits the risk budget.
    If even one lot exceeds the budget the answer is zero, never 'round up'."""
    if lot_size < 1:
        raise ValueError("lot_size must be >= 1")
    if entry <= 0 or stop <= 0:
        return SizeResult(False, 0, 0, 0.0, 0.0, "entry and stop must be positive")
    per_unit = entry - stop if direction == "long" else stop - entry
    if per_unit <= 0:
        return SizeResult(False, 0, 0, 0.0, per_unit,
                          "stop is on the wrong side of entry")
    lots = floor(policy.risk_budget / (per_unit * lot_size))
    if lots < 1:
        one = per_unit * lot_size
        return SizeResult(False, 0, 0, 0.0, per_unit,
                          f"one lot risks ₹{one:,.0f}, above the ₹{policy.risk_budget:,.0f} budget")
    qty = lots * lot_size
    return SizeResult(True, qty, lots, qty * per_unit, per_unit, "within budget")


def reward_risk(entry: float, stop: float, target: float, direction: Direction = "long") -> float:
    risk = entry - stop if direction == "long" else stop - entry
    reward = target - entry if direction == "long" else entry - target
    if risk <= 0:
        raise ValueError("stop is on the wrong side of entry")
    return reward / risk


@dataclass
class ClosedTrade:
    closed_at: datetime
    pnl: float
    planned_stop_loss: float | None = None   # rupee loss if the stop had been honoured


@dataclass
class DailyGuard:
    """Decides whether another recommendation may be shown right now."""
    policy: RiskPolicy
    trades: list[ClosedTrade] = field(default_factory=list)
    opened_today: int = 0

    def _day(self, now: datetime) -> list[ClosedTrade]:
        return [t for t in self.trades if t.closed_at.date() == now.date()]

    def _week(self, now: datetime) -> list[ClosedTrade]:
        monday = now.date() - timedelta(days=now.weekday())
        return [t for t in self.trades if monday <= t.closed_at.date() <= now.date()]

    def status(self, now: datetime) -> dict:
        p = self.policy
        reasons: list[str] = []
        day = self._day(now)
        day_pnl = sum(t.pnl for t in day)
        week_pnl = sum(t.pnl for t in self._week(now))
        if self.opened_today >= p.max_trades_per_day:
            reasons.append(f"daily limit of {p.max_trades_per_day} trades reached")
        if day_pnl <= -p.capital * p.daily_loss_stop_pct / 100:
            reasons.append(f"daily loss stop hit (₹{day_pnl:,.0f})")
        if week_pnl <= -p.capital * p.weekly_loss_stop_pct / 100:
            reasons.append(f"weekly loss stop hit (₹{week_pnl:,.0f})")
        streak = 0
        for t in reversed(day):
            if t.pnl < 0:
                streak += 1
            else:
                break
        if streak >= p.consecutive_loss_lockout:
            reasons.append(f"{streak} losses in a row today; done for the day")
        cooldown_until = None
        if day and day[-1].pnl < 0:
            cooldown_until = day[-1].closed_at + timedelta(minutes=p.cooldown_after_loss_min)
            if now < cooldown_until:
                reasons.append(f"cooling off after a loss until {cooldown_until:%H:%M}")
        return {
            "can_recommend": not reasons,
            "reasons": reasons,
            "trades_opened_today": self.opened_today,
            "day_pnl": day_pnl,
            "week_pnl": week_pnl,
            "remaining_daily_loss": p.capital * p.daily_loss_stop_pct / 100 + min(day_pnl, 0),
            "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
        }


def stop_discipline(trades: list[ClosedTrade]) -> dict:
    """How much money was lost beyond the planned stops (late exits)."""
    over = [(t, -t.pnl - t.planned_stop_loss) for t in trades
            if t.planned_stop_loss is not None and t.pnl < 0 and -t.pnl > t.planned_stop_loss]
    return {"late_exits": len(over), "extra_loss": sum(x for _, x in over)}


# ======================================================================
# quality
# ======================================================================
# Data-quality checks. Any 'error' issue must suppress recommendations.
@dataclass(frozen=True)
class Issue:
    severity: str   # "error" | "warning"
    code: str
    detail: str

    def to_dict(self):
        return asdict(self)


def validate_candles(df: pd.DataFrame, has_volume: bool = True) -> list[Issue]:
    issues: list[Issue] = []
    if df.empty:
        return [Issue("error", "empty", "no candles")]
    if df.index.has_duplicates:
        n = int(df.index.duplicated().sum())
        issues.append(Issue("error", "duplicate_timestamps", f"{n} duplicate candles"))
    if not df.index.is_monotonic_increasing:
        issues.append(Issue("error", "out_of_order", "candles are not in time order"))
    nums = df[["open", "high", "low", "close"]]
    if nums.isna().any().any():
        issues.append(Issue("error", "missing_values", "NaN in OHLC"))
    if (nums <= 0).any().any():
        issues.append(Issue("error", "non_positive_price", "price <= 0"))
    bad_hi = df["high"] < df[["open", "close"]].max(axis=1)
    bad_lo = df["low"] > df[["open", "close"]].min(axis=1)
    if bad_hi.any() or bad_lo.any():
        issues.append(Issue("error", "invalid_ohlc",
                            f"{int(bad_hi.sum() + bad_lo.sum())} candles break high/low bounds"))
    if "volume" in df:
        if (df["volume"] < 0).any():
            issues.append(Issue("error", "negative_volume", "volume < 0"))
        if has_volume and (df["volume"] == 0).all():
            issues.append(Issue("warning", "no_volume", "all volume is zero"))
    return issues


def missing_sessions(daily_index, calendar: ExchangeCalendar) -> list:
    dates = sorted(pd.Timestamp(x).date() for x in daily_index)
    expected = calendar.sessions(dates[0], dates[-1])
    return sorted(set(expected) - set(dates))


def freshness(last_event: datetime, now: datetime, interval: timedelta,
              grace: float = 2.0) -> dict:
    """A feed is stale if no update arrived within `grace` x the bar interval."""
    age = now - last_event
    stale = age > interval * grace
    return {"last_event": last_event.isoformat(), "age_seconds": age.total_seconds(),
            "stale": stale, "blocks_live_recommendations": stale}


# ======================================================================
# analysis
# ======================================================================
# Composite, still deterministic, views: CPR history, width classification,
# regime classification and multi-timeframe summary.
def cpr_history(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily CPR for every session, each built from the PREVIOUS session's HLC,
    plus whether the session left it virgin."""
    rows = []
    prev_lvl = None
    for i in range(1, len(daily)):
        p, cur = daily.iloc[i - 1], daily.iloc[i]
        lvl = cpr(p.high, p.low, p.close)
        rows.append({
            "session": daily.index[i], "pivot": lvl.pivot, "tc": lvl.tc, "bc": lvl.bc,
            "top": lvl.top, "bottom": lvl.bottom, "width_pct": lvl.width_pct,
            "relation": cpr_relation(prev_lvl, lvl).value if prev_lvl else None,
            "virgin": is_virgin(lvl, cur.high, cur.low),
            "open_vs_cpr": price_position(cur.open, lvl),
        })
        prev_lvl = lvl
    return pd.DataFrame(rows).set_index("session")


def classify_width(history_pct: pd.Series, value_pct: float, lookback: int = 250,
                   min_samples: int = 60, narrow_pct: float = 20.0,
                   wide_pct: float = 80.0) -> dict:
    """Narrow/wide relative to this instrument's own recent CPR widths.
    Thresholds are research defaults, not validated edges."""
    h = history_pct.dropna().iloc[-lookback:]
    if len(h) < min_samples:
        return {"label": "insufficient_history", "percentile": None, "samples": len(h)}
    pr = percentile_rank(h, value_pct)
    label = "narrow" if pr <= narrow_pct else "wide" if pr >= wide_pct else "normal"
    return {"label": label, "percentile": pr, "samples": len(h),
            "thresholds": {"narrow_at_or_below": narrow_pct, "wide_at_or_above": wide_pct}}


def regime(daily: pd.DataFrame, adx_trend: float = 25.0, adx_range: float = 18.0,
           atr_lookback: int = 250) -> dict:
    """Rule-based regime v1. Uses only completed bars passed in."""
    if len(daily) < 60:
        return {"label": "uncertain", "version": "regime-v1", "evidence": {"reason": "insufficient history"}}
    h, l, c = daily["high"], daily["low"], daily["close"]
    d = adx_dmi(h, l, c)
    a = atr(h, l, c)
    atr_pct = a / c * 100
    e20, e50 = ema(c, 20), ema(c, 50)
    adx_v, pdi, mdi = d.adx.iloc[-1], d.plus_di.iloc[-1], d.minus_di.iloc[-1]
    atr_rank = percentile_rank(atr_pct.iloc[-atr_lookback:], atr_pct.iloc[-1])
    ev = {"adx": adx_v, "plus_di": pdi, "minus_di": mdi, "atr_pct": atr_pct.iloc[-1],
          "atr_percentile": atr_rank, "close": c.iloc[-1], "ema20": e20.iloc[-1], "ema50": e50.iloc[-1]}
    if np.isnan(adx_v):
        label = "uncertain"
    elif atr_rank >= 90:
        label = "high_volatility"
    elif adx_v >= adx_trend and pdi > mdi and c.iloc[-1] > e50.iloc[-1]:
        label = "trending_bullish"
    elif adx_v >= adx_trend and mdi > pdi and c.iloc[-1] < e50.iloc[-1]:
        label = "trending_bearish"
    elif adx_v <= adx_range and atr_rank <= 25:
        label = "compressed"
    elif adx_v <= adx_range:
        label = "range_bound"
    else:
        label = "transition"
    return {"label": label, "version": "regime-v1", "evidence": ev}


def timeframe_summary(bars: pd.DataFrame, label: str, has_volume: bool) -> dict:
    c = bars["close"]
    out = {"timeframe": label, "bars": len(bars), "last_bar": str(bars.index[-1]),
           "close": float(c.iloc[-1])}
    if len(bars) < 30:
        out["status"] = "insufficient_history"
        return out
    e20, e50 = ema(c, 20), ema(c, 50)
    r = rsi(c)
    d = adx_dmi(bars["high"], bars["low"], c)
    def f(x):
        v = float(x.iloc[-1]); return None if np.isnan(v) else v
    ema20, ema50 = f(e20), f(e50)
    trend = "unknown"
    if ema20 is not None and ema50 is not None:
        if c.iloc[-1] > ema20 > ema50:
            trend = "up"
        elif c.iloc[-1] < ema20 < ema50:
            trend = "down"
        else:
            trend = "mixed"
    rsi_v = f(r)
    out.update({"status": "ok", "ema20": ema20, "ema50": ema50, "rsi14": rsi_v,
                "adx14": f(d.adx), "trend": trend,
                "momentum": None if rsi_v is None else ("bullish" if rsi_v >= 55 else "bearish" if rsi_v <= 45 else "neutral")})
    return out
