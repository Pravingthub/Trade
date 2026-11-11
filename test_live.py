from datetime import datetime, timedelta

import pytest

import pravin_backtest as B
import pravin_live as L
from pravin_quant import IST, ClosedTrade, DailyGuard, RiskPolicy
from test_backtest import synthetic_market

T0 = datetime(2026, 6, 2, 9, 15, tzinfo=IST)


def at(minutes, seconds=0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def test_candle_builder_buckets_and_ignores_outside_hours():
    cb = L.CandleBuilder()
    assert cb.on_tick(T0 - timedelta(minutes=5), 100) == []          # pre-open ignored
    cb.on_tick(at(0, 1), 100)
    cb.on_tick(at(2), 105)
    cb.on_tick(at(4, 59), 98)
    bars = cb.on_tick(at(5, 1), 99)
    assert bars == [{"ts": T0, "open": 100, "high": 105, "low": 98, "close": 98, "ticks": 3}]
    assert cb.flush(at(9)) == [] and len(cb.flush(at(10))) == 1


def test_candle_builder_matches_backtest_bar_convention():
    cb = L.CandleBuilder()
    for k in range(30):
        cb.on_tick(at(k), 100 + k)
    cb.flush(at(31))
    f = cb.frame()
    assert list(f.index[:2]) == [T0, T0 + timedelta(minutes=5)]
    assert f["open"].iloc[1] == 105 and f["close"].iloc[1] == 109


@pytest.fixture
def watcher():
    daily, _ = synthetic_market(n_days=300, seed=5)
    daily = B._date_index(daily)
    day = daily.index[-1]
    w = L.Watcher("NIFTY50", "NIFTY 50", daily.iloc[:-1], day, validated=set(),
                  guard=DailyGuard(RiskPolicy(capital=100_000)), lot_size=65)
    return w


def day_t(w, minutes, seconds=0):
    return datetime(w.day.year, w.day.month, w.day.day, 9, 15, tzinfo=IST) + timedelta(minutes=minutes, seconds=seconds)


def feed(w, prices):
    out = []
    for k, p in enumerate(prices):
        out += w.on_tick(day_t(w, 5 * k, 30), p)
    out += w.check_clock(day_t(w, 5 * len(prices) + 1))
    return out


def test_level_cross_alerts_fire_once(watcher):
    top = watcher.levels["CPR top"]
    alerts = feed(watcher, [top - 20, top + 5, top - 3, top + 6])
    ups = [a for a in alerts if a.key == "CPR top-up"]
    assert len(ups) == 1 and "above CPR top" in ups[0].text


def test_stale_feed_pauses_and_resumes(watcher):
    watcher.on_tick(day_t(watcher, 30), 100)
    a = watcher.check_clock(day_t(watcher, 32))
    assert watcher.stale and a[-1].kind == "feed" and "paused" in a[-1].text
    b = watcher.on_tick(day_t(watcher, 33), 101)
    assert not watcher.stale and "back" in b[0].text


def narrow_long_prices(w):
    c = w.ctx.cpr
    return [c.top + 15, c.top + 16, c.top + 14, c.top + 40, c.top + 41, c.top + 42]


def test_unvalidated_setups_are_silent_by_default(watcher):
    watcher.ctx.width_pct_rank = 5
    alerts = feed(watcher, narrow_long_prices(watcher))
    assert not [a for a in alerts if a.kind == "setup"]


def test_validated_setup_alert_includes_stop_target_and_size(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    alerts = [a for a in feed(watcher, narrow_long_prices(watcher)) if a.kind == "setup"]
    assert len(alerts) == 1
    t = alerts[0].text
    assert "LONG" in t and "Stop" in t and "target" in t and ("lot" in t or "NO TRADE" in t)
    assert "NOT VALIDATED" not in t


def test_unvalidated_shown_only_when_asked_and_labelled(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.show_unvalidated = True
    alerts = [a for a in feed(watcher, narrow_long_prices(watcher)) if a.kind == "setup"]
    assert alerts and alerts[0].text.split(": ", 1)[1].startswith("NOT VALIDATED")


def test_risk_guard_is_shown_on_setup_alerts(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    t = day_t(watcher, 0)
    watcher.guard.trades = [ClosedTrade(t, -300), ClosedTrade(t + timedelta(minutes=1), -300)]
    alerts = feed(watcher, narrow_long_prices(watcher))
    assert not [a for a in alerts if a.kind == "setup"]                 # the stance blocks the setup outright
    assert any(a.kind == "stance" and "DONE FOR DAY" in a.text for a in alerts)


def test_live_and_backtest_agree_on_the_same_bars(watcher):
    """The live watcher must find the same signal the backtester would."""
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    feed(watcher, narrow_long_prices(watcher))
    bars = watcher.builder.frame()
    ctx = B.SessionContext(watcher.day, bars, watcher.ctx.prev, watcher.ctx.cpr, watcher.ctx.pivots,
                           "narrow", 5, watcher.ctx.weekly_cpr, watcher.ctx.regime)
    s = B.get_strategy("CPR-NARROW-OPEN")
    assert s.find(ctx, s.params).signal_bar == 3


def test_live_module_cannot_place_orders():
    src = open(L.__file__).read()
    for word in ("place_order", "modify_order", "cancel_order", "place_gtt"):
        assert word not in src


def test_alerted_setup_is_paper_traded_on_the_next_bar(watcher, tmp_path):
    import pravin_journal as J
    con = J.connect(tmp_path / "j.db")
    watcher.paper = J.PaperBook(con, watcher.guard.policy)
    watcher.guard_fn = lambda now: J.guard_for(con, now, watcher.guard.policy, "paper")
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    watcher.lot_size = 1                                     # small so one lot fits the risk budget
    alerts = feed(watcher, narrow_long_prices(watcher))
    setup = [a for a in alerts if a.kind == "setup"][0]
    assert "Paper trade queued" in setup.text
    papers = [a for a in alerts if a.kind == "paper"]
    assert papers and "opened long" in papers[0].text
    t = con.execute("SELECT * FROM trades").fetchone()
    assert t["setup"] == "CPR-NARROW-OPEN" and t["from_alert"] == 1 and t["entry"] == narrow_long_prices(watcher)[4]


def test_feed_that_never_starts_is_flagged_stale(watcher):
    a = watcher.check_clock(day_t(watcher, 3))
    assert watcher.stale and "no ticks received since the open" in a[-1].text


def test_paper_book_respects_guard(watcher, tmp_path):
    import pravin_journal as J
    con = J.connect(tmp_path / "j.db")
    pol = watcher.guard.policy
    t = day_t(watcher, 0)
    for k in range(2):
        tid = J.open_trade(con, mode="paper", instrument="X", kind="future", side="long", entry=100, stop=90, target=130,
                           qty=1, policy=pol, now=t + timedelta(minutes=40 * k))
        J.close_trade(con, tid, 90, "stop", t + timedelta(minutes=40 * k + 1))
    pb = J.PaperBook(con, pol)
    pb.queue("X", "long", 90, 130, 1, "S", True)
    msgs = pb.on_bar({"ts": t + timedelta(hours=2), "open": 100, "high": 101, "low": 99, "close": 100})
    assert "blocked by the risk guard" in msgs[0]


def test_stance_set_at_open_and_against_stance_setups_suppressed(watcher):
    watcher.ctx.width_pct_rank = 5
    watcher.validated = {"CPR-NARROW-OPEN"}
    alerts = feed(watcher, narrow_long_prices(watcher))
    st = [a for a in alerts if a.kind == "stance"]
    assert st and "LONG BIAS" in st[0].text
    assert any(a.kind == "setup" for a in alerts)
    w2_alerts = []
    watcher2 = watcher
    watcher2.sent.clear()
    watcher2.stance.stance = "STAND_ASIDE"
    watcher2.stance.stand_aside_until = None
    w2_alerts = watcher2._setups(day_t(watcher2, 20))
    assert w2_alerts == []                                   # no setup alert while standing aside


def test_news_and_proxy_change_the_stance(watcher):
    feed(watcher, narrow_long_prices(watcher))
    a = watcher.on_news("high", "check", "HDFC Bank - Financial Results", day_t(watcher, 40))
    assert a and "STAND ASIDE" in a[0].text
    b = watcher.on_proxy("Crude (MCX)", 2.5, 2.0, day_t(watcher, 45))
    assert b == [] or "Crude" in b[0].text                  # already standing aside: no duplicate change


def test_resolve_proxies_picks_nearest_future_and_vix():
    from datetime import date

    import pravin_kite as K

    class Fake:
        def instruments(self, exch):
            if exch == "MCX":
                return [{"name": "CRUDEOIL", "instrument_type": "FUT", "expiry": date(2026, 12, 18), "instrument_token": 2, "tradingsymbol": "CRUDEOIL26DEC"},
                        {"name": "CRUDEOIL", "instrument_type": "FUT", "expiry": date(2026, 11, 19), "instrument_token": 1, "tradingsymbol": "CRUDEOIL26NOV"},
                        {"name": "GOLD", "instrument_type": "FUT", "expiry": date(2026, 10, 1), "instrument_token": 9, "tradingsymbol": "OLD"}]
            if exch == "CDS":
                return [{"name": "USDINR", "instrument_type": "FUT", "expiry": date(2026, 11, 26), "instrument_token": 5, "tradingsymbol": "USDINR26NOV"}]
            return [{"tradingsymbol": "INDIA VIX", "instrument_token": 264969}]
    p = K.resolve_proxies(Fake(), date(2026, 11, 11))
    assert p["Crude (MCX)"]["symbol"] == "CRUDEOIL26NOV" and p["India VIX"]["token"] == 264969
    assert "Gold (MCX)" not in p                             # only an expired contract: not used
