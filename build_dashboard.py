"""Build the static CPR & Pivots dashboard preview.

All numbers are computed by the Python engine via the API service layer and
embedded as JSON; the page itself performs no financial calculations.
Usage: python build_dashboard.py [output.html]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pravin_api import Service, make_provider  # noqa: E402


def build(out: Path, provider: str = "fixture"):
    s = Service(make_provider(provider))
    data = {"status": {"provider": s.p.name, "data_mode": s.p.data_mode.value,
                       "next_session": s.next_session().isoformat(),
                       "last_session": s.p.sessions[-1].isoformat(),
                       "calendar": s.cal.source},
            "instruments": []}
    for inst in s.p.get_instruments():
        i = inst.id
        data["instruments"].append({
            "meta": inst.to_dict(),
            "cpr": {p: s.cpr(i, p) for p in ("day", "week", "month")},
            "pivots": {p: s.pivots(i, p) for p in ("day", "week", "month")},
            "history": s.cpr_history(i, 40),
            "indicators": s.indicators(i, "day"),
            "session": s.session_levels(i),
            "mtf": s.multi_timeframe(i),
        })
    data["health"] = s.data_health()
    tpl = (ROOT / "dashboard_template.html").read_text()
    out.write_text(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(ROOT / "dashboard.html"))
    ap.add_argument("--provider", default="fixture", choices=["fixture", "kite"],
                    help="kite = your local Zerodha history. Keep that page private: do not publish it to GitHub Pages.")
    a = ap.parse_args()
    build(Path(a.out), a.provider)
