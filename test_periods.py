from datetime import date

import pandas as pd
import pytest

from pravin_quant import ExchangeCalendar, IncompletePeriod, InsufficientData, aggregate, opening_range, previous_completed

# Holiday on Monday 2026-01-26 (a weekday national holiday).
CAL = ExchangeCalendar(holidays=frozenset({date(2026, 1, 26)}), source="test")


def make_daily(start, end, cal=CAL):
    rows = {}
    for i, d in enumerate(cal.sessions(start, end)):
        base = 100 + i
        rows[d] = dict(open=base, high=base + 5, low=base - 5, close=base + 1, volume=1000 + i)
    return pd.DataFrame.from_dict(rows, orient="index")


DAILY = make_daily(date(2026, 1, 1), date(2026, 3, 6))


def test_daily_uses_previous_session_across_weekend():
    src = previous_completed(DAILY, date(2026, 1, 19), "day", CAL)   # Monday
    assert src.end == date(2026, 1, 16)                               # Friday


def test_daily_uses_previous_session_across_holiday():
    src = previous_completed(DAILY, date(2026, 1, 27), "day", CAL)   # Tue after holiday
    assert src.end == date(2026, 1, 23)


def test_weekly_holiday_week_has_four_sessions_and_is_complete():
    src = previous_completed(DAILY, date(2026, 2, 2), "week", CAL)
    assert (src.start, src.end, src.sessions) == (date(2026, 1, 27), date(2026, 1, 30), 4)
    week = DAILY.loc[[date(2026, 1, d) for d in (27, 28, 29, 30)]]
    assert src.high == week.high.max() and src.low == week.low.min()
    assert src.open == week.open.iloc[0] and src.close == week.close.iloc[-1]


def test_weekly_midweek_still_uses_last_completed_week():
    src = previous_completed(DAILY, date(2026, 2, 4), "week", CAL)   # Wednesday
    assert src.end == date(2026, 1, 30)


def test_monthly_uses_previous_calendar_month():
    src = previous_completed(DAILY, date(2026, 2, 10), "month", CAL)
    assert (src.start, src.end) == (date(2026, 1, 1), date(2026, 1, 30))
    assert src.sessions == len(CAL.sessions(date(2026, 1, 1), date(2026, 1, 31)))


def test_no_look_ahead_current_and_future_data_ignored():
    as_of = date(2026, 2, 4)
    poisoned = DAILY.copy()
    for d in poisoned.index:
        if d >= as_of:
            poisoned.loc[d, ["high", "low"]] = [99999, 1]
    for period in ("day", "week", "month"):
        assert previous_completed(poisoned, as_of, period, CAL) == \
               previous_completed(DAILY, as_of, period, CAL)


def test_missing_session_in_week_is_refused():
    gappy = DAILY.drop(date(2026, 1, 28))
    with pytest.raises(IncompletePeriod, match="2026-01-28"):
        previous_completed(gappy, date(2026, 2, 2), "week", CAL)


def test_stale_daily_data_is_refused():
    with pytest.raises(IncompletePeriod):
        previous_completed(DAILY.iloc[:-3], date(2026, 3, 9), "day", CAL)


def test_no_history_raises():
    with pytest.raises(InsufficientData):
        previous_completed(DAILY, date(2026, 1, 1), "day", CAL)


def test_aggregate_weeks_follow_exchange_weeks():
    w = aggregate(DAILY, "week")
    assert w.loc[date(2026, 1, 30), "sessions"] == 4
    assert w["sessions"].max() == 5


def test_opening_range_complete_and_partial():
    idx = pd.date_range("2026-02-02 09:15", periods=6, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame({"open": 10, "high": [11, 12, 13, 20, 21, 22], "low": [9, 8, 7, 1, 1, 1],
                       "close": 10, "volume": 1}, index=idx)
    assert opening_range(df, date(2026, 2, 2), 15) == {"minutes": 15, "high": 13.0, "low": 7.0}
    assert opening_range(df.iloc[:2], date(2026, 2, 2), 15) is None
