import plistlib
from datetime import date

import pravin_schedule as S


def test_plists_are_valid_weekday_only_and_ist(tmp_path):
    paths = S.install("kite", agents=tmp_path, python="/usr/bin/python3", load=False)
    assert len(paths) == len(S.JOBS)
    brief = plistlib.loads((tmp_path / "com.pravinai.brief.plist").read_bytes())
    assert brief["ProgramArguments"][-4:] == ["run", "brief", "--provider", "kite"]
    assert {d["Weekday"] for d in brief["StartCalendarInterval"]} == {1, 2, 3, 4, 5}
    assert all((d["Hour"], d["Minute"]) == (8, 50) for d in brief["StartCalendarInterval"])
    assert brief["EnvironmentVariables"]["TZ"] == "Asia/Kolkata"
    news = plistlib.loads((tmp_path / "com.pravinai.news.plist").read_bytes())
    assert len(news["StartCalendarInterval"]) == 5 * (1 + 14)     # 07:45 + every 30 min 09:00-15:30


def calls():
    log = []
    return log, (lambda args: log.append(args) or 0), (lambda t: log.append(("notify", t)))


def test_holidays_and_weekends_are_skipped():
    log, runner, notify = calls()
    assert S.run("brief", "kite", date(2026, 11, 24), runner, notify, True).startswith("skipped")   # Guru Nanak Jayanti
    assert S.run("brief", "kite", date(2026, 11, 14), runner, notify, True).startswith("skipped")   # Saturday
    assert log == []


def test_brief_refreshes_data_when_logged_in():
    log, runner, notify = calls()
    S.run("brief", "kite", date(2026, 11, 12), runner, notify, has_token=True)
    assert log[0][:2] == ["pravin_kite.py", "fetch"] and log[1][0] == "pravin_brief.py"


def test_without_login_brief_warns_and_live_does_not_start():
    log, runner, notify = calls()
    S.run("brief", "kite", date(2026, 11, 12), runner, notify, has_token=False)
    assert log[0][0] == "notify" and "yesterday" in log[0][1]
    assert S.run("live", "kite", date(2026, 11, 12), runner, notify, has_token=False) == "not started: no Kite login"
    assert S.run("login", "kite", date(2026, 11, 12), runner, notify, has_token=False) == "reminder sent"


def test_live_runs_with_paper_trading_when_logged_in():
    log, runner, notify = calls()
    S.run("live", "kite", date(2026, 11, 12), runner, notify, has_token=True)
    assert log == [["pravin_live.py", "--paper"]]
