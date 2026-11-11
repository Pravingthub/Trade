from datetime import date, datetime, timedelta

import pandas as pd

from pravin_quant import ExchangeCalendar, freshness, missing_sessions, validate_candles


def df(rows, idx=None):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                        index=idx or range(len(rows)))


def codes(issues):
    return {i.code for i in issues}


def test_clean_data_has_no_issues():
    assert validate_candles(df([[10, 11, 9, 10.5, 5], [10.5, 12, 10, 11, 6]])) == []


def test_detects_invalid_ohlc_duplicates_and_order():
    bad = df([[10, 9.5, 9, 10, 5], [10, 11, 9, 10, 5]], idx=[2, 1])
    assert {"invalid_ohlc", "out_of_order"} <= codes(validate_candles(bad))
    dup = df([[10, 11, 9, 10, 5]] * 2, idx=[1, 1])
    assert "duplicate_timestamps" in codes(validate_candles(dup))


def test_index_without_volume_is_flagged():
    d = df([[10, 11, 9, 10, 0]] * 3)
    assert "no_volume" in codes(validate_candles(d, has_volume=True))
    assert validate_candles(d, has_volume=False) == []


def test_missing_sessions_against_calendar():
    cal = ExchangeCalendar(holidays=frozenset({date(2026, 1, 26)}))
    idx = [date(2026, 1, d) for d in (22, 23, 27, 29)]
    assert missing_sessions(idx, cal) == [date(2026, 1, 28)]


def test_stale_feed_blocks_recommendations():
    now = datetime(2026, 2, 2, 11, 0)
    assert not freshness(now - timedelta(minutes=4), now, timedelta(minutes=5))["stale"]
    s = freshness(now - timedelta(minutes=11), now, timedelta(minutes=5))
    assert s["stale"] and s["blocks_live_recommendations"]
