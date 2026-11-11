from datetime import date, datetime, timedelta

import pytest

import pravin_journal as J
from pravin_quant import IST, RiskPolicy

POL = RiskPolicy(capital=100_000)
T0 = datetime(2026, 6, 2, 10, 0, tzinfo=IST)


@pytest.fixture
def con(tmp_path):
    return J.connect(tmp_path / "j.db")


def op(con, now=T0, **kw):
    args = dict(mode="paper", instrument="NIFTY", kind="option", side="long", entry=120, stop=100, target=160, qty=65,
                setup="CPR-NARROW-OPEN", validated=True, policy=POL, now=now)
    args.update(kw)
    return J.open_trade(con, **args)


def test_open_and_close_with_charges_and_r(con):
    tid = op(con)
    t = J.close_trade(con, tid, 160, "target", T0 + timedelta(minutes=40))
    assert t["gross"] == pytest.approx(40 * 65)
    expected_ch = J.options_charges(120 * 65, 160 * 65, T0.date())["total"]
    assert t["charges"] == pytest.approx(expected_ch)
    assert t["r_multiple"] == pytest.approx((2600 - expected_ch) / (20 * 65))


def test_short_trade_and_validation(con):
    tid = op(con, side="short", kind="future", entry=25000, stop=25050, target=24900, qty=65)
    t = J.close_trade(con, tid, 24900, "target", T0 + timedelta(hours=1))
    assert t["gross"] == pytest.approx(100 * 65)
    with pytest.raises(ValueError):
        op(con, side="long", entry=100, stop=110)
    with pytest.raises(ValueError):
        J.close_trade(con, tid, 1, "manual")


def test_guard_state_is_rebuilt_from_journal(con):
    for k in range(2):
        tid = op(con, now=T0 + timedelta(minutes=40 * k))
        J.close_trade(con, tid, 100, "stop", T0 + timedelta(minutes=40 * k + 5))
    g = J.guard_for(con, T0 + timedelta(hours=2), POL, "paper").status(T0 + timedelta(hours=2))
    assert not g["can_recommend"] and any("in a row" in r for r in g["reasons"])
    # a third PAPER trade is refused; a LIVE trade (already at the broker) is recorded and flagged
    with pytest.raises(J.GuardBlocked):
        op(con, now=T0 + timedelta(hours=2))
    tid = op(con, now=T0 + timedelta(hours=2), mode="live")
    assert con.execute("SELECT guard_ok FROM trades WHERE id=?", (tid,)).fetchone()[0] == 1   # live guard is separate


def test_review_measures_discipline(con):
    a = op(con)                                            # honoured stop
    J.close_trade(con, a, 100, "stop", T0 + timedelta(minutes=30))
    b = op(con, now=T0 + timedelta(hours=1), setup="discretionary", validated=False)
    J.close_trade(con, b, 80, "manual", T0 + timedelta(hours=2))   # late: stop was 100, exited 80
    c = op(con, now=T0 + timedelta(hours=3), mode="live")      # after two losses: live trade recorded, flagged
    J.close_trade(con, c, 160, "target", T0 + timedelta(hours=4))
    rv = J.review(con, T0.date())
    d = rv["discipline"]
    assert d["late_stop_exits"] == 1 and d["late_stop_cost"] == pytest.approx(20 * 65)
    assert d["off_plan_trades"] == 1 and d["off_plan_net"] < 0
    assert rv["by_mode"]["live"]["trades"] == 1
    assert rv["by_setup"]["CPR-NARROW-OPEN"]["trades"] == 2
    md = J.review_markdown(rv)
    assert "Late stop exits: 1" in md and "| discretionary |" in md


def test_overtrading_days_counted(con):
    for k in range(5):
        tid = op(con, now=T0 + timedelta(minutes=10 * k), mode="live")
        J.close_trade(con, tid, 125, "manual", T0 + timedelta(minutes=10 * k + 5))
    assert J.review(con, T0.date())["discipline"]["days_over_trade_limit"] == 1


def test_equity_charges_by_hand():
    c = J.charges_for("equity", 100_000, 101_000, date(2026, 6, 2))
    brok = 20 + 20
    txn = 0.0000297 * 201_000
    sebi = 0.000001 * 201_000
    assert c == pytest.approx(brok + 0.00025 * 101_000 + txn + sebi + 3 + 0.18 * (brok + txn + sebi))


def bar(minutes, o, h, low, c):
    return {"ts": datetime(2026, 6, 2, 9, 15, tzinfo=IST) + timedelta(minutes=minutes), "open": o, "high": h, "low": low, "close": c}


def test_paper_book_fills_like_the_backtester(con):
    pb = J.PaperBook(con, POL)
    pb.queue("NIFTY", "long", stop=990, target=1030, qty=65, setup="S", validated=True)
    m1 = pb.on_bar(bar(20, 1005, 1010, 1000, 1008))           # fill at this bar's open
    assert "opened long at 1,005.00" in m1[0]
    m2 = pb.on_bar(bar(25, 1008, 1035, 985, 1020))           # both hit: stop first
    assert "stop" in m2[0]
    t = con.execute("SELECT * FROM trades").fetchone()
    assert t["entry"] == 1005 and t["exit"] == 990 and t["from_alert"] == 1


def test_paper_book_time_exit_and_gap(con):
    pb = J.PaperBook(con, POL)
    pb.queue("NIFTY", "short", stop=1010, target=950, qty=65, setup="S", validated=True)
    pb.on_bar(bar(30, 1000, 1002, 998, 999))
    out = pb.on_bar(bar(360, 990, 991, 989, 990))              # 15:15 bar
    assert "15:15" in out[0]
    pb.queue("NIFTY", "long", stop=990, target=1030, qty=65, setup="S", validated=True)
    assert "skipped" in pb.on_bar(bar(30, 985, 986, 984, 985))[0]   # opens through the stop
