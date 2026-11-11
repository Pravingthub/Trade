"""Run Vriddhi automatically on trading days (macOS launchd).

Jobs (IST, Monday to Friday, skipped on NSE holidays from nse_holidays.csv):
    07:45  news     fetch official announcements (again every 30 min during market hours)
    08:15  login    Telegram reminder to log in to Kite (Zerodha requires a manual login daily)
    08:50  brief    morning brief (+ Telegram)
    09:10  live     live watcher until 15:30 (paper-trades alerts into the journal)
    16:00  review   journal review of the day (+ Telegram)

    python pravin_schedule.py install --provider kite     # writes and loads the launchd jobs
    python pravin_schedule.py status
    python pravin_schedule.py uninstall
    python pravin_schedule.py run brief                   # what each job executes (manual test)

Your Mac must be awake. To wake it each weekday morning:
    sudo pmset repeat wakeorpoweron MTWRF 07:40:00
Login itself cannot be automated: Zerodha requires you to log in yourself, and
Vriddhi never stores your password or TOTP secret.
"""
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from pravin_quant import IST, ExchangeCalendar

ROOT = Path(__file__).resolve().parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL = "com.vriddhi.{}"

JOBS = {
    "news": [(7, 45)] + [(h, m) for h in range(9, 16) for m in (0, 30) if (h, m) <= (15, 30)],
    "login": [(8, 15)],
    "brief": [(8, 50)],
    "live": [(9, 10)],
    "review": [(16, 0)],
}


def trading_calendar() -> ExchangeCalendar:
    from pravin_kite import read_holiday_file
    e = read_holiday_file()
    return ExchangeCalendar(holidays=frozenset(x.day for x in e if x.kind in ("holiday", "muhurat")),
                            extra_sessions=frozenset(x.day for x in e if x.kind == "special_session"),
                            source="nse_holidays.csv")


def plist_for(job: str, python: str, provider: str) -> dict:
    return {
        "Label": LABEL.format(job),
        "ProgramArguments": [python, str(ROOT / "pravin_schedule.py"), "run", job, "--provider", provider],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": [{"Weekday": wd, "Hour": h, "Minute": m} for wd in range(1, 6) for h, m in JOBS[job]],
        "StandardOutPath": str(ROOT / "logs" / f"{job}.log"),
        "StandardErrorPath": str(ROOT / "logs" / f"{job}.log"),
        "EnvironmentVariables": {"TZ": "Asia/Kolkata", "PRAVIN_DATA_PROVIDER": provider},
    }


def install(provider: str, agents: Path = AGENTS, python: str | None = None, load=True) -> list[Path]:
    python = python or sys.executable
    agents.mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    paths = []
    for job in JOBS:
        p = agents / f"{LABEL.format(job)}.plist"
        p.write_bytes(plistlib.dumps(plist_for(job, python, provider)))
        paths.append(p)
        if load:
            subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
            subprocess.run(["launchctl", "load", str(p)], check=True)
    return paths


def uninstall(agents: Path = AGENTS) -> None:
    for job in JOBS:
        p = agents / f"{LABEL.format(job)}.plist"
        if p.exists():
            subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
            p.unlink()


def run(job: str, provider: str, today: date | None = None, runner=None, notify=None, has_token=None) -> str:
    """Execute one job if today is a trading day. Returns what happened (for logs and tests)."""
    today = today or datetime.now(IST).date()
    if not trading_calendar().is_session(today):
        return "skipped: not a trading day"
    from pravin_kite import load_token
    has_token = has_token if has_token is not None else (load_token() is not None)
    py = sys.executable
    runner = runner or (lambda args: subprocess.run([py, *args], cwd=ROOT).returncode)
    if notify is None:
        from pravin_brief import telegram_from_env
        from pravin_kite import load_env
        tg = telegram_from_env(load_env())
        notify = (lambda t: tg.send(t, dedupe_key=f"{today}-{job}-{t[:40]}")) if tg else print
    needs_kite = provider == "kite"
    if job == "login":
        if needs_kite and not has_token:
            notify("Vriddhi: log in to Kite for today. On your Mac run: python pravin_kite.py login")
            return "reminder sent"
        return "already logged in"
    if job == "news":
        return f"exit {runner(['pravin_news.py', 'fetch'])}"
    if job == "brief":
        if needs_kite and has_token:
            runner(["pravin_kite.py", "fetch", "--years", "0.05"])
        elif needs_kite:
            notify("Vriddhi: not logged in to Kite, so the brief uses yesterday's stored data and may be incomplete.")
        return f"exit {runner(['pravin_brief.py', '--provider', provider, '--telegram'])}"
    if job == "live":
        if needs_kite and not has_token:
            notify("Vriddhi: live alerts NOT started because you are not logged in to Kite today.")
            return "not started: no Kite login"
        if not needs_kite:
            return "not started: live alerts need Kite data"
        return f"exit {runner(['pravin_live.py', '--paper'])}"
    if job == "review":
        import pravin_journal as J
        rv = J.review(J.connect(), today)
        notify(J.review_markdown(rv)[:3800])
        return "review sent"
    raise ValueError(job)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--provider", default="kite", choices=["kite", "fixture"])
    sub.add_parser("uninstall")
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("job", choices=list(JOBS))
    r.add_argument("--provider", default="kite", choices=["kite", "fixture"])
    a = ap.parse_args(argv)
    if a.cmd == "install":
        for p in install(a.provider):
            print("installed", p.name)
        print("Logs: logs/<job>.log. Keep the Mac awake on weekday mornings (see --help).")
    elif a.cmd == "uninstall":
        uninstall()
        print("removed all Vriddhi jobs")
    elif a.cmd == "status":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for job in JOBS:
            print(job, "loaded" if LABEL.format(job) in out else "not loaded")
    else:
        print(datetime.now(IST).isoformat(timespec="seconds"), a.job, run(a.job, a.provider))


if __name__ == "__main__":
    main()
