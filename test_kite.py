"""Zerodha module tests with a fake Kite client (no network, no credentials)."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import pravin_kite as K
from pravin_quant import IST


class FakeKite:
    """Mimics kiteconnect.KiteConnect for the calls we use, and enforces the
    documented max-days-per-request limits so chunking bugs fail loudly."""

    def __init__(self, sessions, holidays=()):
        self.calls = []
        self.sessions = sessions
        self.holidays = set(holidays)

    def instruments(self, exchange=None):
        return [{"tradingsymbol": "NIFTY 50", "instrument_token": 256265, "exchange": "NSE"},
                {"tradingsymbol": "NIFTY BANK", "instrument_token": 260105, "exchange": "NSE"},
                {"tradingsymbol": "RELIANCE", "instrument_token": 738561, "exchange": "NSE"}]

    def historical_data(self, token, from_date, to_date, interval, continuous=False, oi=False):
        limits = {"5minute": 100, "day": 2000}
        assert (to_date - from_date).days + 1 <= limits[interval], "interval exceeds max limit"
        self.calls.append((from_date, to_date, interval))
        rows = []
        for d in self.sessions:
            if from_date <= d <= to_date:
                base = 20000 + (d - self.sessions[0]).days
                if interval == "day":
                    rows.append({"date": datetime(d.year, d.month, d.day, tzinfo=IST), "open": base, "high": base + 50,
                                 "low": base - 50, "close": base + 10, "volume": 0})
                else:
                    for k in range(75):
                        t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * k)
                        rows.append({"date": t, "open": base, "high": base + 50 - abs(37 - k),
                                     "low": base - 50 + abs(37 - k), "close": base + 10, "volume": 0})
        return rows


def sessions_between(a, b, holidays=()):
    return [d.date() for d in pd.date_range(a, b, freq="B") if d.date() not in set(holidays)]


HOL = [date(2026, 1, 26), date(2026, 3, 3)]
SESS = sessions_between("2025-11-03", "2026-03-31", HOL)


def test_date_chunks_respect_limits_and_cover_range():
    ch = K.date_chunks(date(2024, 1, 1), date(2024, 12, 31), "5minute")
    assert ch[0][0] == date(2024, 1, 1) and ch[-1][1] == date(2024, 12, 31)
    assert all((e - s).days + 1 <= 100 for s, e in ch)
    assert all(ch[i][1] + timedelta(days=1) == ch[i + 1][0] for i in range(len(ch) - 1))


def test_fetch_history_chunks_and_dedupes():
    fk = FakeKite(SESS, HOL)
    df = K.fetch_history(fk, 256265, date(2025, 11, 3), date(2026, 3, 31), "5minute",
                         sleep=lambda s: None, log=lambda *a: None)
    assert len(fk.calls) == 2
    assert len(df) == len(SESS) * 75 and df.index.is_unique and df.index.is_monotonic_increasing
    assert str(df.index.tz) == "Asia/Kolkata"


def test_resolve_tokens_from_instrument_master():
    assert K.resolve_tokens(FakeKite(SESS)) == {"NIFTY50": 256265, "BANKNIFTY": 260105}


def test_resolve_tokens_refuses_ambiguity():
    class Dup(FakeKite):
        def instruments(self, exchange=None):
            return super().instruments() * 2
    with pytest.raises(RuntimeError):
        K.resolve_tokens(Dup(SESS))


def test_token_expires_at_6am_ist(tmp_path):
    p = tmp_path / "tok"
    K.save_token("abc", p)
    now = datetime.now(IST)
    assert K.load_token(p, now) == "abc"
    tomorrow_7am = (now + timedelta(days=1)).replace(hour=7, minute=0)
    assert K.load_token(p, tomorrow_7am) is None


def test_env_loader(tmp_path, monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("# comment\nKITE_API_KEY=abc123\nOTHER='x'\n")
    env = K.load_env(tmp_path / ".env")
    assert env["KITE_API_KEY"] == "abc123" and env["OTHER"] == "x"


def test_store_roundtrip_and_merge(tmp_path):
    st = K.LocalStore(tmp_path)
    fk = FakeKite(SESS)
    a = K.fetch_history(fk, 1, date(2025, 11, 3), date(2025, 12, 31), "day", sleep=lambda s: None, log=lambda *a: None)
    b = K.fetch_history(fk, 1, date(2025, 12, 15), date(2026, 3, 31), "day", sleep=lambda s: None, log=lambda *a: None)
    st.save("NIFTY50", "day", a)
    st.save("NIFTY50", "day", b)
    got = st.load("NIFTY50", "day")
    assert len(got) == len(SESS) and got.index.is_unique
    assert got["close"].iloc[-1] == b["close"].iloc[-1]


def test_calendar_from_data_infers_holidays_and_uses_file_for_future():
    idx = pd.Index(pd.to_datetime(SESS))
    daily = pd.DataFrame({"close": 1.0}, index=idx)
    entries = [K.CalendarEntry(date(2026, 4, 3), "holiday", "Good Friday"),
               K.CalendarEntry(date(2026, 2, 1), "special_session", "Budget")]
    cal = K.calendar_from_data(daily, entries)
    assert not cal.is_session(date(2026, 1, 26)) and not cal.is_session(date(2026, 3, 3))
    assert cal.is_session(date(2026, 1, 27))
    assert not cal.is_session(date(2026, 4, 3))            # future holiday from file
    assert not cal.is_session(date(2026, 2, 1))            # past Sunday with no candle in data


def test_muhurat_session_never_becomes_previous_day():
    days = sessions_between("2026-10-01", "2026-11-06", [date(2026, 10, 2), date(2026, 10, 20)]) + \
        [date(2026, 11, 8), date(2026, 11, 9)]
    daily = pd.DataFrame({"close": 1.0}, index=pd.Index(pd.to_datetime(days)))
    entries = [K.CalendarEntry(date(2026, 11, 8), "muhurat", "Muhurat"),
               K.CalendarEntry(date(2026, 11, 10), "holiday", "Balipratipada")]
    cal = K.calendar_from_data(daily, entries)
    assert not cal.is_session(date(2026, 11, 8))
    assert cal.previous_session(date(2026, 11, 9)) == date(2026, 11, 6)
    assert cal.previous_session(date(2026, 11, 11)) == date(2026, 11, 9)


@pytest.fixture
def store_provider(tmp_path, monkeypatch):
    st = K.LocalStore(tmp_path)
    fk = FakeKite(SESS, HOL)
    for iid in ("NIFTY50", "BANKNIFTY"):
        for iv in ("day", "5minute"):
            st.save(iid, iv, K.fetch_history(fk, 1, SESS[0], SESS[-1], iv, sleep=lambda s: None, log=lambda *a: None))
    monkeypatch.setattr(K, "HOLIDAY_FILE", tmp_path / "none.csv")
    return K.StoreProvider(st)


def test_store_provider_feeds_the_real_service(store_provider):
    from pravin_api import Service
    s = Service(store_provider)
    r = s.cpr("NIFTY50", "day", date(2026, 1, 27))           # day after Republic Day
    assert r["source_ohlc"]["end"] == "2026-01-23"
    assert r["provenance"]["data_mode"] == "historical" and r["provenance"]["demo"] is False
    w = s.cpr("NIFTY50", "week", date(2026, 3, 9))           # Holi week -> 4 sessions
    assert w["source_ohlc"]["sessions"] == 4


def test_daily_vs_intraday_consistency_check(store_provider):
    diffs = store_provider.daily_from_intraday_check("NIFTY50")
    assert (diffs < 1e-9).all()


def test_module_has_no_order_functions():
    src = open(K.__file__).read()
    for word in ("place_order", "modify_order", "cancel_order", "place_gtt"):
        assert word not in src


def test_dashboard_builds_from_real_data_store(tmp_path, monkeypatch, store_provider):
    import json as _json

    import build_dashboard
    monkeypatch.setattr(K, "DATA_DIR", store_provider.store.root)
    out = tmp_path / "dash.html"
    build_dashboard.build(out, "kite")
    html = out.read_text()
    data = _json.loads(html.split("const DATA = ")[1].split(";\n")[0])
    assert data["status"]["data_mode"] == "historical"
    assert {i["meta"]["id"] for i in data["instruments"]} == {"NIFTY50", "BANKNIFTY"}
    assert data["instruments"][0]["session"]["vwap"]["value"] is None     # index: no volume, never faked
