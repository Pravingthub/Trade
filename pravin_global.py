"""Global markets before the Indian open, from FRED (Federal Reserve Bank of
St. Louis): free, official, with an API key you create at fred.stlouisfed.org.

FRED publishes daily closes, usually with a short lag, so every value carries
its own observation date and is flagged stale when it is older than the last
completed US session. Intraday global moves during Indian hours come from
Indian-listed proxies via Zerodha (MCX crude and gold, USD/INR, India VIX).

    python pravin_global.py            # fetch and print the overnight picture
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "global.json"
API = "https://api.stlouisfed.org/fred/series/observations"

# FRED series ids. 'risk' = how a rise is usually read for Indian equities.
SERIES = {
    "SP500": ("S&P 500", "up_is_supportive"),
    "NASDAQCOM": ("Nasdaq Composite", "up_is_supportive"),
    "NIKKEI225": ("Nikkei 225", "up_is_supportive"),
    "VIXCLS": ("US VIX", "up_is_adverse"),
    "DGS10": ("US 10-year yield", "up_is_adverse"),
    "DCOILBRENTEU": ("Brent crude", "up_is_adverse"),
    "DEXINUS": ("USD/INR", "up_is_adverse"),
}
# Moves that count as a shock (percent; for yields, percentage points).
SHOCK = {"SP500": 1.5, "NASDAQCOM": 2.0, "NIKKEI225": 2.0, "VIXCLS": 15.0, "DGS10": 0.15,
         "DCOILBRENTEU": 3.0, "DEXINUS": 0.5}


def fetch_series(sid: str, api_key: str, getter=None) -> list[tuple[date, float]]:
    """Latest observations, newest first, skipping FRED's '.' placeholders."""
    q = urllib.parse.urlencode({"series_id": sid, "api_key": api_key, "file_type": "json",
                                "sort_order": "desc", "limit": 10})
    raw = (getter or _get)(f"{API}?{q}")
    obs = json.loads(raw)["observations"]
    out = [(date.fromisoformat(o["date"]), float(o["value"])) for o in obs if o["value"] not in (".", "")]
    if len(out) < 2:
        raise ValueError(f"{sid}: fewer than two observations")
    return out


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Vriddhi"}), timeout=20) as r:
        return r.read()


def last_us_session(today_ist: date) -> date:
    """Most recent weekday before today (US holidays are not modelled; a value
    one day older than this is treated as possibly delayed, not as an error)."""
    d = today_ist - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def summarise(rows: dict[str, list[tuple[date, float]]], today_ist: date) -> dict:
    expect = last_us_session(today_ist)
    items, supportive, adverse, shocks = [], 0, 0, []
    for sid, obs in rows.items():
        name, risk = SERIES[sid]
        (d1, v1), (_, v0) = obs[0], obs[1]
        is_yield = sid == "DGS10"
        chg = (v1 - v0) if is_yield else (v1 / v0 - 1) * 100
        stale = (expect - d1).days > 3
        direction = 0 if abs(chg) < (0.02 if is_yield else 0.25) else (1 if chg > 0 else -1)
        effect = direction * (1 if risk == "up_is_supportive" else -1)
        if not stale:
            supportive += effect > 0
            adverse += effect < 0
        shock = abs(chg) >= SHOCK[sid] and not stale
        if shock:
            shocks.append(f"{name} {chg:+.2f}{' pts' if is_yield else '%'}")
        items.append({"id": sid, "name": name, "date": d1.isoformat(), "value": v1, "change": chg,
                      "unit": "pts" if is_yield else "%", "effect": "supportive" if effect > 0 else "adverse" if effect < 0 else "neutral",
                      "stale": stale, "shock": shock})
    tone = "risk-on" if supportive >= adverse + 2 else "risk-off" if adverse >= supportive + 2 else "mixed"
    return {"as_of": today_ist.isoformat(), "tone": "shock" if shocks else tone, "shocks": shocks,
            "supportive": int(supportive), "adverse": int(adverse), "items": items,
            "source": "FRED (Federal Reserve Bank of St. Louis), daily closes"}


def fetch_all(api_key: str, today_ist: date, getter=None) -> dict:
    rows, errors = {}, {}
    for sid in SERIES:
        try:
            rows[sid] = fetch_series(sid, api_key, getter)
        except Exception as e:                       # one failing series never stops the rest
            errors[sid] = str(e)[:120]
    out = summarise(rows, today_ist) if rows else {"as_of": today_ist.isoformat(), "tone": "unavailable", "items": [],
                                                    "shocks": [], "supportive": 0, "adverse": 0}
    out["errors"] = errors
    return out


def save(summary: dict, path: Path = CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**summary, "fetched_utc": datetime.now(timezone.utc).isoformat()}, indent=1))


def load(path: Path = CACHE) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def main():
    from pravin_kite import load_env
    from pravin_quant import IST
    key = load_env().get("FRED_API_KEY")
    if not key:
        raise SystemExit("Add FRED_API_KEY to .env (free key from fred.stlouisfed.org → My Account → API Keys).")
    s = fetch_all(key, datetime.now(IST).date())
    save(s)
    print(f"Global tone: {s['tone']}  ({s['supportive']} supportive, {s['adverse']} adverse)")
    for i in s["items"]:
        print(f"  {i['name']:<18} {i['value']:>10,.2f}  {i['change']:+.2f}{i['unit']}  {i['effect']:<10} {i['date']}"
              + ("  STALE" if i["stale"] else "") + ("  SHOCK" if i["shock"] else ""))
    for k, v in s.get("errors", {}).items():
        print(f"  {k}: failed ({v})")


if __name__ == "__main__":
    main()
