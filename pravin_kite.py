"""Zerodha Kite Connect integration: login, historical download, local store,
exchange calendar from real data, and a provider the API can use.

READ-ONLY. This module never places, modifies or cancels orders.

Commands (run on your own computer, inside the project folder):
    python pravin_kite.py login                 # once per day; Kite tokens expire daily
    python pravin_kite.py fetch --years 6       # NIFTY 50 + NIFTY BANK, day and 5-minute
    python pravin_kite.py status                # what is stored locally

Secrets: KITE_API_KEY and KITE_API_SECRET go in a local `.env` file that is
never committed. The daily access token is saved to `.kite_token` (also never
committed).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from pravin_quant import IST, ExchangeCalendar, daily_from_intraday, resample_intraday

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TOKEN_FILE = ROOT / ".kite_token"
HOLIDAY_FILE = ROOT / "nse_holidays.csv"

# Maximum date span per historical request, per Zerodha's documented limits.
# Kept slightly under the limit; change here if Zerodha changes them.
MAX_DAYS = {"minute": 55, "3minute": 95, "5minute": 95, "10minute": 95,
            "15minute": 195, "30minute": 195, "60minute": 395, "day": 1900}
REQUESTS_PER_SECOND = 3          # documented historical-API rate limit

DEFAULT_INSTRUMENTS = {          # our id -> (exchange, Kite tradingsymbol)
    "NIFTY50": ("NSE", "NIFTY 50"),
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
}


# ---------------------------------------------------------------- config
def load_env(path: Path = ROOT / ".env") -> dict:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables win."""
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    out.update({k: v for k, v in os.environ.items() if k.startswith(("KITE_", "PRAVIN_", "TELEGRAM_", "OPENAI_", "AI_"))})
    return out


def save_token(access_token: str, path: Path = TOKEN_FILE) -> None:
    path.write_text(json.dumps({"access_token": access_token,
                                "saved_ist": datetime.now(IST).isoformat()}))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_token(path: Path = TOKEN_FILE, now: datetime | None = None) -> str | None:
    """Kite access tokens expire every morning (around 6 AM IST). A token saved
    before today's 6 AM is treated as expired so the user is asked to log in."""
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    saved = datetime.fromisoformat(d["saved_ist"])
    now = (now or datetime.now(IST)).astimezone(IST)
    cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    return d["access_token"] if saved >= cutoff else None


def kite_client(env: dict | None = None, require_token: bool = True):
    from kiteconnect import KiteConnect  # imported lazily: only needed with real credentials
    env = env or load_env()
    key = env.get("KITE_API_KEY")
    if not key:
        raise SystemExit("KITE_API_KEY is missing. Add it to your local .env file.")
    kite = KiteConnect(api_key=key)
    if require_token:
        tok = load_token()
        if not tok:
            raise SystemExit("No valid Kite session for today. Run: python pravin_kite.py login")
        kite.set_access_token(tok)
    return kite


def login(env: dict | None = None) -> None:
    env = env or load_env()
    secret = env.get("KITE_API_SECRET")
    if not secret:
        raise SystemExit("KITE_API_SECRET is missing. Add it to your local .env file (never to GitHub).")
    kite = kite_client(env, require_token=False)
    print("1. Open this link and log in to Zerodha:\n  ", kite.login_url())
    print("2. After login you are redirected to your app's redirect URL.")
    print("   Copy the value of request_token from that address bar.")
    rt = input("Paste request_token here: ").strip()
    session = kite.generate_session(rt, api_secret=secret)
    save_token(session["access_token"])
    print("Logged in. Token saved locally for today in .kite_token")


# ---------------------------------------------------------------- download
def date_chunks(start: date, end: date, interval: str) -> list[tuple[date, date]]:
    span = MAX_DAYS[interval]
    out, s = [], start
    while s <= end:
        e = min(s + timedelta(days=span - 1), end)
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


def resolve_tokens(kite, wanted: dict = DEFAULT_INSTRUMENTS) -> dict[str, int]:
    """Look instrument tokens up from Kite's instrument master (never hardcoded)."""
    by_exchange: dict[str, list] = {}
    out = {}
    for iid, (exch, sym) in wanted.items():
        if exch not in by_exchange:
            by_exchange[exch] = kite.instruments(exch)
        match = [r for r in by_exchange[exch] if r["tradingsymbol"] == sym]
        if len(match) != 1:
            raise RuntimeError(f"Could not uniquely resolve {exch}:{sym} ({len(match)} matches)")
        out[iid] = int(match[0]["instrument_token"])
    return out


def candles_to_frame(rows: list[dict], interval: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    ts = pd.to_datetime(df["date"])
    ts = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
    df = df.assign(ts=ts).set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def fetch_history(kite, token: int, start: date, end: date, interval: str,
                  sleep=time.sleep, log=print) -> pd.DataFrame:
    frames = []
    chunks = date_chunks(start, end, interval)
    for k, (s, e) in enumerate(chunks, 1):
        rows = kite.historical_data(token, s, e, interval)
        frames.append(candles_to_frame(rows, interval))
        log(f"  {interval} {s} to {e}: {len(rows)} candles ({k}/{len(chunks)})")
        sleep(1.0 / REQUESTS_PER_SECOND + 0.05)
    df = pd.concat(frames) if frames else candles_to_frame([], interval)
    return df[~df.index.duplicated(keep="last")].sort_index()


FUTURES_NAME = {"NIFTY50": "NIFTY", "BANKNIFTY": "BANKNIFTY"}


def current_lot_sizes(kite, ids, today: date | None = None) -> dict[str, int]:
    """Lot size of the nearest unexpired future, from Kite's NFO instrument master.
    Lot sizes change over time; this is today's value only."""
    today = today or datetime.now(IST).date()
    rows = kite.instruments("NFO")
    out = {}
    for iid in ids:
        name = FUTURES_NAME.get(iid)
        futs = [r for r in rows if r.get("name") == name and r.get("instrument_type") == "FUT"
                and r.get("expiry") and r["expiry"] >= today]
        if futs:
            out[iid] = int(min(futs, key=lambda r: r["expiry"])["lot_size"])
    return out


# ---------------------------------------------------------------- local store
class LocalStore:
    """Candles stored as gzip CSV: data/<ID>_<interval>.csv.gz. Not committed."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or DATA_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, iid: str, interval: str) -> Path:
        return self.root / f"{iid}_{interval}.csv.gz"

    def save(self, iid: str, interval: str, df: pd.DataFrame) -> None:
        old = self.load(iid, interval)
        if not old.empty:
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        out = df.copy()
        out.index = out.index.tz_convert(IST).strftime("%Y-%m-%dT%H:%M:%S%z")
        out.index.name = "ts"
        out.to_csv(self.path(iid, interval), compression="gzip")
        meta = self.root / "meta.json"
        m = json.loads(meta.read_text()) if meta.exists() else {}
        m[f"{iid}_{interval}"] = {"saved_at": datetime.now(timezone.utc).isoformat(),
                                  "rows": len(df), "first": str(df.index[0]), "last": str(df.index[-1])}
        meta.write_text(json.dumps(m, indent=1))

    def load(self, iid: str, interval: str) -> pd.DataFrame:
        p = self.path(iid, interval)
        if not p.exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.read_csv(p, index_col="ts")
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
        return df

    def set_meta(self, key: str, value) -> None:
        meta = self.root / "meta.json"
        m = json.loads(meta.read_text()) if meta.exists() else {}
        m[key] = value
        meta.write_text(json.dumps(m, indent=1))

    def meta(self) -> dict:
        p = self.root / "meta.json"
        return json.loads(p.read_text()) if p.exists() else {}


# ---------------------------------------------------------------- calendar
@dataclass(frozen=True)
class CalendarEntry:
    day: date
    kind: str
    name: str


def read_holiday_file(path: Path | None = None) -> list[CalendarEntry]:
    path = path or HOLIDAY_FILE
    if not path.exists():
        return []
    rows = [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    out = []
    for r in csv.DictReader(rows):
        out.append(CalendarEntry(date.fromisoformat(r["date"]), r["type"].strip(), r["name"].strip()))
    return out


def calendar_from_data(index_daily: pd.DataFrame, entries: list[CalendarEntry]) -> ExchangeCalendar:
    """Sessions in history = days on which the index actually printed a daily
    candle (the exchange's own record). Future dates use the holiday file.
    Muhurat sessions are excluded so a one-hour session never becomes the
    'previous day' for CPR."""
    days = sorted(pd.Timestamp(d).date() for d in index_daily.index)
    first, last = days[0], days[-1]
    have = set(days)
    muhurat = {e.day for e in entries if e.kind == "muhurat"}
    hol = {d for d in pd.date_range(first, last, freq="B").date if d not in have}
    extra = {d for d in have if d.weekday() >= 5 and d not in muhurat}
    for e in entries:
        if e.day > last:
            if e.kind == "holiday":
                hol.add(e.day)
            elif e.kind == "special_session":
                extra.add(e.day)
    hol |= {d for d in muhurat if d.weekday() < 5}
    return ExchangeCalendar(holidays=frozenset(hol), extra_sessions=frozenset(extra),
                            source=f"sessions from Zerodha NIFTY 50 daily candles {first}..{last}; "
                                   f"future dates from nse_holidays.csv")


# ---------------------------------------------------------------- provider
class StoreProvider:
    """MarketDataProvider over the local store (real, historical data)."""
    name = "zerodha-kite-historical"

    def __init__(self, store: LocalStore | None = None, ids=("NIFTY50", "BANKNIFTY")):
        from pravin_api import DataMode, Instrument  # avoid a circular import at module load
        self.data_mode = DataMode.HISTORICAL
        self.store = store or LocalStore()
        self._daily, self._intraday, self._instruments = {}, {}, []
        entries = read_holiday_file()
        self._muhurat = {e.day for e in entries if e.kind == "muhurat"}
        for iid in ids:
            d = self.store.load(iid, "day")
            m = self.store.load(iid, "5minute")
            if d.empty:
                continue
            d = d[[ts.date() not in self._muhurat for ts in d.index]]
            d.index = pd.Index([ts.date() for ts in d.index])
            self._daily[iid] = d
            self._intraday[iid] = m[[ts.date() not in self._muhurat for ts in m.index]] if not m.empty else m
            name = DEFAULT_INSTRUMENTS.get(iid, ("NSE", iid))[1]
            self._instruments.append(Instrument(iid, name, name, "NSE", "index", False, 0.05, None, None))
        if "NIFTY50" not in self._daily:
            raise RuntimeError("No NIFTY50 daily data in the local store. Run: python pravin_kite.py fetch")
        self.calendar = calendar_from_data(self._daily["NIFTY50"], entries)
        self.sessions = [d for d in self._daily["NIFTY50"].index]
        self.generated_at = datetime.now(timezone.utc)

    def get_instruments(self):
        return list(self._instruments)

    def instrument(self, iid):
        for i in self._instruments:
            if i.id == iid:
                return i
        raise KeyError(iid)

    def get_historical_candles(self, iid, interval="day", start=None, end=None):
        self.instrument(iid)
        if interval == "day":
            df = self._daily[iid]
        elif interval == "5minute":
            df = self._intraday[iid]
        elif interval in ("15minute", "60minute"):
            df = resample_intraday(self._intraday[iid], int(interval.replace("minute", ""))).drop(columns="bars")
        else:
            raise ValueError(interval)
        if start or end:
            keys = df.index if interval == "day" else pd.Index(df.index.tz_convert(IST).date)
            mask = [(start is None or k >= start) and (end is None or k <= end) for k in keys]
            df = df[mask]
        return df

    def last_update(self, iid):
        m = self._intraday.get(iid)
        if m is not None and not m.empty:
            return (m.index[-1] + pd.Timedelta(minutes=5)).to_pydatetime()
        return datetime.combine(self._daily[iid].index[-1], datetime.min.time(), IST)

    def daily_from_intraday_check(self, iid) -> pd.DataFrame:
        """Compare Kite daily candles with daily bars rebuilt from 5-minute data.
        Large gaps reveal missing intraday data."""
        d = self._daily[iid]
        rebuilt = daily_from_intraday(self._intraday[iid])
        j = d.join(rebuilt, rsuffix="_5m", how="inner")
        return (j[["high", "low", "close"]].values - j[["high_5m", "low_5m", "close_5m"]].values).__abs__().max(axis=0)


# ---------------------------------------------------------------- CLI
def cmd_fetch(years: float, ids: list[str], intervals: list[str]) -> None:
    kite = kite_client()
    tokens = resolve_tokens(kite, {i: DEFAULT_INSTRUMENTS[i] for i in ids})
    store = LocalStore()
    lots = current_lot_sizes(kite, ids)
    store.set_meta("lot_sizes", lots)
    print(f"Current futures lot sizes from Kite: {lots}")
    end = datetime.now(IST).date()
    start = end - timedelta(days=int(365 * years))
    for iid, tok in tokens.items():
        for iv in intervals:
            print(f"{iid} ({tok}) {iv}")
            df = fetch_history(kite, tok, start, end, iv)
            if df.empty:
                print("  no data returned")
                continue
            store.save(iid, iv, df)
            print(f"  stored {len(df)} candles, {df.index[0].date()} to {df.index[-1].date()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    f = sub.add_parser("fetch")
    f.add_argument("--years", type=float, default=6)
    f.add_argument("--ids", default="NIFTY50,BANKNIFTY")
    f.add_argument("--intervals", default="day,5minute")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "login":
        login()
    elif a.cmd == "fetch":
        cmd_fetch(a.years, a.ids.split(","), a.intervals.split(","))
    elif a.cmd == "status":
        m = LocalStore().meta()
        print(json.dumps(m, indent=1) if m else "Nothing stored yet. Run: python pravin_kite.py fetch")


if __name__ == "__main__":
    sys.exit(main())
