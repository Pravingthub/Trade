"""Independent check: our implementations vs TA-Lib, a separate C library.
Runs on synthetic data and on gappy, trending and flat segments."""
import numpy as np
import pandas as pd
import pytest

talib = pytest.importorskip("talib")
import pravin_quant as I

TOL = 1e-8


def make(seed, n=800, drift=0.0, vol=80.0):
    rng = np.random.default_rng(seed)
    c = 20000 + np.cumsum(rng.normal(drift, vol, n))
    gaps = rng.normal(0, vol * 3, n) * (rng.random(n) < 0.05)   # occasional gaps
    c = c + np.cumsum(gaps)
    o = c - rng.normal(0, vol / 2, n)
    h = np.maximum(o, c) + rng.uniform(1, vol, n)
    l = np.minimum(o, c) - rng.uniform(1, vol, n)
    v = rng.integers(10_000, 2_000_000, n).astype(float)
    return [pd.Series(x) for x in (o, h, l, c, v)]


DATASETS = {"random": make(1), "uptrend": make(2, drift=25), "downtrend": make(3, drift=-25),
            "quiet": make(4, vol=5)}


def same(a, b, start=0):
    a, b = np.asarray(a, float)[start:], np.asarray(b, float)[start:]
    assert (np.isnan(a) == np.isnan(b)).all(), "warm-up (NaN) positions differ"
    m = ~np.isnan(a)
    np.testing.assert_allclose(a[m], b[m], rtol=0, atol=TOL * max(1, np.nanmax(np.abs(b))))


@pytest.mark.parametrize("name", DATASETS)
def test_parity(name):
    o, h, l, c, v = DATASETS[name]
    H, L, C, V = h.values, l.values, c.values, v.values
    for n in (20, 50, 200):
        same(I.ema(c, n), talib.EMA(C, n))
    same(I.rsi(c, 14), talib.RSI(C, 14))
    same(I.atr(h, l, c, 14), talib.ATR(H, L, C, 14))
    d = I.adx_dmi(h, l, c, 14)
    same(d.plus_di, talib.PLUS_DI(H, L, C, 14))
    same(d.minus_di, talib.MINUS_DI(H, L, C, 14))
    same(d.adx, talib.ADX(H, L, C, 14))
    bb = I.bollinger(c, 20, 2)
    up, mid, lo = talib.BBANDS(C, 20, 2, 2, 0)
    same(bb.upper, up); same(bb.middle, mid); same(bb.lower, lo)
    same(I.obv(c, v), talib.OBV(C, V))
    same(I.mfi(h, l, c, v, 14), talib.MFI(H, L, C, V, 14))


@pytest.mark.parametrize("name", DATASETS)
def test_macd_converges_to_talib_after_warmup(name):
    _, _, _, c, _ = DATASETS[name]
    m = I.macd(c)
    tl, ts, th = talib.MACD(c.values, 12, 26, 9)
    same(m.macd, tl, start=150); same(m.signal, ts, start=150)
