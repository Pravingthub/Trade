from datetime import date

import pytest
from fastapi.testclient import TestClient

from pravin_api import app, svc
from pravin_quant import cpr

client = TestClient(app)


def test_health_and_no_order_routes():
    assert client.get("/health").json()["order_placement"] == "disabled"
    paths = [r.path.lower() for r in app.routes]
    assert not any(w in p for p in paths for w in ("order", "trade/place", "execute", "gtt"))
    assert all(set(getattr(r, "methods", {"GET"})) <= {"GET", "HEAD"} for r in app.routes
               if r.path.startswith("/api"))


def test_everything_is_labelled_demo():
    r = client.get("/api/v1/analytics/cpr/NIFTY50").json()
    assert r["provenance"]["demo"] is True and r["provenance"]["data_mode"] == "simulated"
    assert client.get("/api/v1/market/status").json()["demo"] is True


def test_cpr_endpoint_matches_engine_on_visible_source():
    r = client.get("/api/v1/analytics/cpr/NIFTY50?period=day").json()
    s = r["source_ohlc"]
    expected = cpr(s["high"], s["low"], s["close"])
    assert r["levels"]["pivot"] == pytest.approx(expected.pivot)
    assert r["levels"]["top"] == pytest.approx(expected.top)
    assert date.fromisoformat(s["end"]) < date.fromisoformat(r["applies_to"])


@pytest.mark.parametrize("period", ["day", "week", "month"])
def test_periods_use_completed_sessions_only(period):
    r = client.get(f"/api/v1/analytics/cpr/BANKNIFTY?period={period}").json()
    assert date.fromisoformat(r["source_ohlc"]["end"]) < date.fromisoformat(r["applies_to"])
    if period == "week":
        assert r["source_ohlc"]["sessions"] <= 5


def test_weekly_source_matches_daily_candles():
    r = client.get("/api/v1/analytics/cpr/RELIANCE?period=week").json()["source_ohlc"]
    daily = svc().p.get_historical_candles("RELIANCE", "day")
    wk = daily[(daily.index >= date.fromisoformat(r["start"])) & (daily.index <= date.fromisoformat(r["end"]))]
    assert r["high"] == wk.high.max() and r["low"] == wk.low.min() and r["close"] == wk.close.iloc[-1]


def test_pivots_endpoint():
    r = client.get("/api/v1/analytics/pivots/NIFTY50?camarilla_multiplier=1.0").json()
    assert r["camarilla"]["multiplier"] == 1.0
    assert r["floor"]["r1"] > r["floor"]["pivot"] > r["floor"]["s1"]


def test_index_volume_indicators_are_not_faked():
    r = client.get("/api/v1/analytics/indicators/NIFTY50").json()
    assert r["indicators"]["volume"]["status"] == "unavailable"
    assert r["regime"]["label"] in {"trending_bullish", "trending_bearish", "range_bound",
                                    "high_volatility", "compressed", "transition", "uncertain"}


def test_index_vwap_comes_from_future():
    r = client.get("/api/v1/analytics/session-levels/NIFTY50").json()
    assert r["vwap"]["source_instrument"] == "NIFTYFUT"
    assert r["opening_range_15m"]["high"] >= r["opening_range_15m"]["low"]


def test_multi_timeframe_excludes_current_week():
    r = client.get("/api/v1/analytics/multi-timeframe/NIFTY50").json()
    week = next(f for f in r["timeframes"] if f["timeframe"] == "week")
    assert date.fromisoformat(week["last_bar"][:10]) < date.fromisoformat(r["applies_to"])


def test_position_size_no_trade_paths():
    big = client.get("/api/v1/risk/position-size", params=dict(
        capital=100000, entry=120, stop=100, lot_size=75)).json()
    assert big["ok"] is False and big["quantity"] == 0
    weak = client.get("/api/v1/risk/position-size", params=dict(
        capital=100000, entry=100, stop=98, target=102)).json()
    assert weak["ok"] is False and "reward/risk" in weak["reason"]


def test_calculations_refuse_rather_than_guess():
    r = client.get("/api/v1/analytics/cpr/NIFTY50?as_of=2024-11-07")
    assert r.status_code == 422 and r.json()["status"] == "no_data"


def test_data_health_blocks_live_recommendations_in_demo():
    r = client.get("/api/v1/data-health").json()
    assert all(not i["live_recommendations_allowed"] for i in r["instruments"])


def test_unknown_instrument_404():
    assert client.get("/api/v1/analytics/cpr/NOPE").status_code == 404
