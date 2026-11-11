import numpy as np
import pandas as pd
import pytest

import pravin_quant as I


def test_ema_hand_example():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    e = I.ema(s, 3)                       # seed = mean(1,2,3)=2, alpha=0.5
    assert np.isnan(e.iloc[1])
    assert e.iloc[2:].tolist() == pytest.approx([2.0, 3.0, 4.0, 5.0])


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert I.rsi(up).iloc[-1] == 100.0
    flat = pd.Series(np.full(40, 5.0))
    assert I.rsi(flat).iloc[-1] == 50.0


def test_true_range_uses_previous_close_gap():
    h = pd.Series([10.0, 12.0]); l = pd.Series([9.0, 11.5]); c = pd.Series([9.5, 11.8])
    assert I.true_range(h, l, c).iloc[1] == pytest.approx(2.5)   # 12 - 9.5 gap


def test_vwap_hand_example_and_session_reset():
    idx = pd.to_datetime(["2026-02-02 09:15", "2026-02-02 09:20", "2026-02-03 09:15"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"high": [102, 104, 200], "low": [98, 100, 196], "close": [100, 103, 198],
                       "volume": [10, 30, 5]}, index=idx)
    v = I.vwap(df)
    tp1, tp2 = 100.0, (104 + 100 + 103) / 3
    assert v.iloc[0] == pytest.approx(tp1)
    assert v.iloc[1] == pytest.approx((tp1 * 10 + tp2 * 30) / 40)
    assert v.iloc[2] == pytest.approx(198.0)   # reset at new session


def test_vwap_refuses_zero_volume_index():
    idx = pd.date_range("2026-02-02 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame({"high": 2.0, "low": 1.0, "close": 1.5, "volume": 0.0}, index=idx)
    assert I.vwap(df).isna().all()


def test_bollinger_uses_population_std():
    s = pd.Series([2, 4, 4, 4, 5, 5, 7, 9], dtype=float)   # population sd = 2
    b = I.bollinger(s, n=8, k=2)
    assert b.middle.iloc[-1] == 5 and b.upper.iloc[-1] == 9 and b.lower.iloc[-1] == 1


def test_relative_volume_excludes_current_bar():
    v = pd.Series([100.0] * 20 + [300.0])
    assert I.relative_volume(v, 20).iloc[-1] == 3.0
