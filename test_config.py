import json
from datetime import date

import pytest

import pravin_config as C
from pravin_quant import ExchangeCalendar


def cfg(**risk):
    c = C.load()
    c["risk"].update(risk)
    return c


def test_default_config_loads_and_validates():
    p = C.risk_policy()
    assert p.capital == 100_000 and p.risk_budget == 1_000


@pytest.mark.parametrize("bad", [dict(capital=200_000), dict(capital=40_000), dict(risk_per_trade_pct=5),
                                 dict(max_trades_per_day=10), dict(weekly_loss_stop_pct=1.0), dict(min_reward_risk=0.5)])
def test_loose_or_out_of_range_limits_are_refused(bad):
    with pytest.raises(C.ConfigError):
        C.risk_policy(cfg(**bad))


def test_policy_summary_computes_every_displayed_number():
    s = C.policy_summary(C.risk_policy(cfg(capital=50_000)), trades_opened_today=2)
    assert s == {"capital": 50_000, "per_trade": 500, "daily_stop": 1_000, "weekly_stop": 2_500, "max_trades": 3,
                 "trades_left": 1, "min_reward_risk": 2.0, "cooldown_min": 30, "loss_lockout": 2}


def test_lot_size_unverified_until_kite_supplies_it():
    c = C.contract("NIFTY50")
    assert c.lot_size is None and not c.lot_verified
    v = C.contract("NIFTY50", store_meta={"lot_sizes": {"NIFTY50": 75}})
    assert v.lot_size == 75 and v.lot_verified


CAL = ExchangeCalendar(holidays=frozenset({date(2026, 11, 24)}))


def test_expiry_rules_and_holiday_shift():
    assert C.next_expiry("weekly_tuesday", date(2026, 11, 12), CAL.is_session) == date(2026, 11, 17)
    assert C.next_expiry("weekly_tuesday", date(2026, 11, 18), CAL.is_session) == date(2026, 11, 23)  # 24th holiday
    assert C.next_expiry("monthly_last_tuesday", date(2026, 11, 12), CAL.is_session) == date(2026, 11, 23)
    assert C.next_expiry("monthly_last_tuesday", date(2026, 11, 26), CAL.is_session) == date(2026, 12, 29)


def test_config_file_is_valid_json_with_no_secrets():
    raw = C.CONFIG_PATH.read_text()
    json.loads(raw)
    assert "secret" not in raw.lower() and "api_key" not in raw.lower()
