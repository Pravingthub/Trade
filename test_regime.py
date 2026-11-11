import numpy as np
import pandas as pd
import pytest

import pravin_regime as R
from pravin_api import Service, make_provider


def bars(closes):
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c})


def test_trend_label_rules():
    assert R.trend_label(bars(np.linspace(100, 200, 80)))["label"] == "up"
    assert R.trend_label(bars(np.linspace(200, 100, 80)))["label"] == "down"
    assert R.trend_label(bars(100 + np.sin(np.arange(80)) * 0.5))["label"] == "range"
    assert R.trend_label(bars(range(30)))["label"] == "insufficient"


def L(st, i, s, h):
    return {"short_term": {"label": st}, "intraday": {"label": i}, "swing": {"label": s}, "higher": {"label": h}}


@pytest.mark.parametrize("layers,label,phrase", [
    (L("up", "up", "up", "up"), "uptrend", "agree"),
    (L("down", "down", "up", "up"), "uptrend", "pullback against the uptrend"),
    (L("up", "up", "up", "down"), "conflicted", "counter-trend"),
    (L("up", "range", "range", "up"), "range", "lean up"),
    (L("up", "up", "volatile", "up"), "high_volatility", "top decile"),
    (L("up", "up", "down", "mixed"), "downtrend", "lacks higher-timeframe backing"),
])
def test_overall_rules_explain_disagreement(layers, label, phrase):
    o = R.overall(layers)
    assert o["overall"] == label and any(phrase in n for n in o["explanation"])


def test_stack_on_service_uses_completed_bars_only():
    s = Service(make_provider("fixture"))
    as_of = s.next_session()
    st = R.stack(s, "NIFTY50", as_of)
    assert set(st["layers"]) == {"short_term", "intraday", "swing", "higher"}
    for d in (st["layers"]["short_term"]["detail"]["5m"], st["layers"]["intraday"]["detail"]["1h"]):
        assert d["last_bar"][:10] < as_of.isoformat()
    assert st["explanation"] and st["version"] == R.VERSION
