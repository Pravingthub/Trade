"""Market read: global markets + official announcements + events → one overnight lean.

Transparent points, never a probability:
  global (fresh values only): S&P 500 ×2, Nasdaq, Nikkei, US VIX, US 10-year,
      Brent, USD/INR ×1 each; +weight if the move is supportive for Indian
      equities, −weight if adverse; a shock counts double
  announcements in the last 24 hours: high impact ±3, medium ±1, low 0
  lean: ≥ +3 bullish · ≤ −3 bearish · otherwise neutral
Events add cautions, not points.

The lean is CONTEXT for the day, not a trade signal and not validated by any
backtest. The open and the CPR triggers still decide; the lean says which side
has the wind behind it and warns when a trade goes against it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

WEIGHT = {"SP500": 2, "NASDAQCOM": 1, "NIKKEI225": 1, "VIXCLS": 1, "DGS10": 1, "DCOILBRENTEU": 1, "DEXINUS": 1}
NEWS_POINTS = {"high": 3, "medium": 1, "low": 0}


def read(global_summary: dict | None, news: list[dict], events: list[dict], now: datetime) -> dict:
    drivers, cautions, score = [], [], 0
    g = global_summary or {}
    stale = []
    for i in g.get("items", []):
        if i["stale"]:
            stale.append(i["name"])
            continue
        w = WEIGHT.get(i["id"], 1) * (2 if i.get("shock") else 1)
        pts = w if i["effect"] == "supportive" else -w if i["effect"] == "adverse" else 0
        score += pts
        if pts:
            drivers.append({"source": "global", "points": pts,
                            "text": f"{i['name']} {i['change']:+.2f}{' pts' if i['unit'] == 'pts' else '%'} ({i['effect']})"})
    if not g.get("items"):
        cautions.append("Global markets not fetched: the overnight picture is incomplete.")
    elif stale:
        cautions.append("Stale, not counted: " + ", ".join(stale) + ".")
    if g.get("shocks"):
        cautions.append("Overnight shock: " + ", ".join(g["shocks"]) + ". Let the open settle before trading.")
    cutoff = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    notable = 0
    for a in news:
        if (a.get("published_utc") or "") < cutoff or a.get("bias") in (None, "routine"):
            continue
        p = NEWS_POINTS.get(a.get("impact"), 0)
        pts = p if a["bias"] == "supportive" else -p if a["bias"] == "adverse" else 0
        if a["bias"] in ("check", "mixed") and a.get("impact") == "high":
            cautions.append(f"Read before trading: {a['publisher']}: {a['title'][:100]}")
        if pts:
            score += pts
            notable += 1
            drivers.append({"source": "news", "points": pts, "text": f"{a['publisher']}: {a['title'][:90]}"})
    for e in events:
        if e.get("impact") == "high":
            cautions.append(f"{e['event'].split(' (')[0]} affects the {e['affects_session']} session"
                            + (" (today)." if e.get("today") else "."))
    lean = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
    drivers.sort(key=lambda d: -abs(d["points"]))
    if lean == "neutral":
        use = ("No overnight reason to favour either side. Let the open and the CPR triggers decide; "
               "take whichever side triggers.")
        if score <= -1:
            use += " The tilt is slightly negative, so be a little quicker to respect stops on longs."
        elif score >= 1:
            use += " The tilt is slightly positive, so be a little quicker to respect stops on shorts."
    else:
        side, other = ("long", "short") if lean == "bullish" else ("short", "long")
        use = (f"The overnight wind favours {side}s. A {side} trigger has context behind it; "
               f"a {other} trigger goes against it, so treat it with extra caution or skip it.")
    quiet_news = not notable
    summary = (f"Overnight lean: {lean.upper()} ({score:+d}). "
               + ("Global markets: " + "; ".join(d["text"] for d in drivers if d["source"] == "global")[:220] + ". "
                  if any(d["source"] == "global" for d in drivers) else "")
               + ("Nothing market-moving from regulators in the last 24 hours. " if quiet_news else ""))
    return {"lean": lean, "score": score, "summary": summary.strip(), "drivers": drivers,
            "cautions": cautions, "how_to_use": use,
            "method": "transparent points, not a probability; context for the day, not a trade signal"}
