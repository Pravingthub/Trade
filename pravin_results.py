"""Reads official results (India CPI) after release, and refuses to guess.

A CPI figure is ACCEPTED only when every check passes:
  1. the headline sentence names "All India Consumer Price Index (CPI)" (so food
     inflation, CFPI, and the Labour Bureau's CPI-IW can never be taken instead)
  2. every headline sentence in the release gives the SAME number
  3. the release's month and year are the month we are waiting for
  4. at least one independent confirmation and no contradiction:
       - the "Highlights" line states the same number, and/or
       - the number lies between the rural and urban rates (it must)
  5. it is plausible: between -5% and 20%, and within 3 points of last month
Otherwise the status is NEEDS_CHECK with the reason, and nothing changes.

    python pravin_results.py            # check sources for any released, unread event
"""
from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data" / "results.json"
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]

NUM = r"(-?\d{1,2}\.\d{1,2})\s?%"
HEADLINE = re.compile(r"Year-on-year inflation rate based on All India Consumer Price Index \(CPI\)"
                      r"(?: with base year \d{4})? for the month of ([A-Za-z]+),? (\d{4}) over [A-Za-z]+,? \d{4} is " + NUM,
                      re.I)
HIGHLIGHT = re.compile(r"Retail inflation based on Consumer Price Index in ([A-Za-z]+),? (\d{4}) is " + NUM, re.I)
RURAL_URBAN = re.compile(r"Corresponding inflation rates for rural and urban are " + NUM + r" and " + NUM, re.I)


@dataclass
class Reading:
    status: str                      # READ | NEEDS_CHECK
    value: float | None = None
    month: str | None = None
    year: int | None = None
    rural: float | None = None
    urban: float | None = None
    checks: list = field(default_factory=list)
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def normalise(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", text)
    t = t.replace("\u00a0", " ").replace("per cent", "%").replace("percent", "%")
    return re.sub(r"\s+", " ", t)


def extract_cpi(text: str, month: str, year: int, previous: float | None = None) -> Reading:
    t = normalise(text)
    month = month.lower()
    heads = list(HEADLINE.finditer(t))
    if not heads:
        return Reading("NEEDS_CHECK", reason="headline CPI sentence not found (food, CPI-IW and other figures are never used)")
    values = {float(m.group(3)) for m in heads}
    if len(values) > 1:
        return Reading("NEEDS_CHECK", reason=f"headline sentences disagree: {sorted(values)}")
    h = heads[0]
    v, m_txt, y_txt = float(h.group(3)), h.group(1).lower(), int(h.group(2))
    checks = [f"headline sentence says {v}%"]
    if m_txt != month or y_txt != year:
        return Reading("NEEDS_CHECK", v, m_txt, y_txt, checks=checks,
                       reason=f"release is for {m_txt.title()} {y_txt}, waiting for {month.title()} {year}")
    checks.append(f"month matches ({month.title()} {year})")
    confirmations = 0
    hl = [m for m in HIGHLIGHT.finditer(t) if m.group(1).lower() == month and int(m.group(2)) == year]
    if hl:
        hv = {float(m.group(3)) for m in hl}
        if hv != {v}:
            return Reading("NEEDS_CHECK", v, month, year, checks=checks, reason=f"highlights say {sorted(hv)}, headline says {v}")
        confirmations += 1
        checks.append("highlights line agrees")
    rural = urban = None
    ru = RURAL_URBAN.search(t, h.end(), h.end() + 300)
    if ru and ru.start() - h.end() > 30:                     # must be the sentence immediately after the headline,
        ru = None                                            # never a later one (e.g. food inflation's rural/urban rates)
    if ru:
        rural, urban = float(ru.group(1)), float(ru.group(2))
        if not (min(rural, urban) - 0.01 <= v <= max(rural, urban) + 0.01):
            return Reading("NEEDS_CHECK", v, month, year, rural, urban, checks,
                           reason=f"{v}% is not between rural {rural}% and urban {urban}%")
        confirmations += 1
        checks.append(f"lies between rural {rural}% and urban {urban}%")
    if confirmations == 0:
        return Reading("NEEDS_CHECK", v, month, year, checks=checks, reason="no independent confirmation in the release")
    if not (-5 <= v <= 20):
        return Reading("NEEDS_CHECK", v, month, year, rural, urban, checks, reason=f"{v}% is outside the plausible range")
    if previous is not None and abs(v - previous) > 3:
        return Reading("NEEDS_CHECK", v, month, year, rural, urban, checks,
                       reason=f"{v}% is more than 3 points from last month's {previous}%: confirm by hand")
    return Reading("READ", v, month, year, rural, urban, checks)


def judge_cpi(actual: float, previous: float | None, expected: float | None) -> dict:
    """Hotter than the reference = bad for markets (more rate hikes likely)."""
    ref, label = (expected, "expected") if expected is not None else (previous, "last month")
    if ref is None:
        return {"bias": "check", "text": f"CPI {actual:.2f}%: no reference to compare with."}
    d = actual - ref
    if d >= 0.15:
        bias, word = "adverse", "hotter"
    elif d <= -0.15:
        bias, word = "supportive", "cooler"
    else:
        bias, word = "neutral", "in line"
    effect = {"adverse": "bad for markets ↓ (more rate hikes likely)", "supportive": "good for markets ↑ (less pressure to hike)",
              "neutral": "little reaction expected"}[bias]
    note = "" if expected is not None else " (no consensus forecast entered, so compared with last month)"
    return {"bias": bias, "text": f"CPI {actual:.2f}% vs {ref:.2f}% {label}: {word} → {effect}{note}."}


# ---------------------------------------------------------------- discovery and storage
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Vriddhi (personal research)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(15_000_000)


def page_text(raw: bytes) -> str:
    if raw[:5] == b"%PDF-":
        from io import BytesIO

        from pypdf import PdfReader
        return " ".join((p.extract_text() or "") for p in PdfReader(BytesIO(raw)).pages[:4])
    return raw.decode("utf-8", errors="ignore")


def find_cpi_links(feed_xml: bytes, month: str, year: int) -> list[str]:
    root = ET.fromstring(feed_xml.lstrip(b"\xef\xbb\xbf").strip())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").lower()
        if "consumer price index" in title and "industrial workers" not in title and "rural labour" not in title \
                and month.lower() in title and str(year) in title:
            out.append((it.findtext("link") or "").strip())
    return [u for u in out if u]


def load(path: Path = STORE) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save(d: dict, path: Path = STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=1))


def event_key(e: dict) -> str:
    return f"{e['date']}|{e['event']}"


def cpi_month(event_name: str) -> tuple[str, int] | None:
    m = re.search(r"cpi.*?for (" + "|".join(MONTHS) + r") (\d{4})", event_name, re.I)
    return (m.group(1).lower(), int(m.group(2))) if m else None


def check_released(events: list[dict], now: datetime, sources: list[str], getter=_get, store: dict | None = None) -> dict:
    """For each released CPI event without a result: try the sources; store READ or NEEDS_CHECK."""
    store = store if store is not None else load()
    for e in events:
        target = cpi_month(e.get("event", ""))
        if not target or event_key(e) in store and store[event_key(e)]["status"] == "READ":
            continue
        rel = datetime.fromisoformat(f"{e['date']}T{(e.get('time_ist') or '00:00')}:00+05:30")
        if now < rel:
            continue
        prev = float(e["previous"]) if e.get("previous") else None
        exp = float(e["expected"]) if e.get("expected") else None
        reading = Reading("NEEDS_CHECK", reason="release not found in the configured sources yet")
        for src in sources:
            try:
                for link in find_cpi_links(getter(src), *target):
                    reading = extract_cpi(page_text(getter(link)), *target, previous=prev)
                    if reading.status == "READ":
                        break
            except Exception as ex:                     # a broken source never stops the others
                reading = Reading("NEEDS_CHECK", reason=f"source failed: {str(ex)[:120]}")
            if reading.status == "READ":
                break
        rec = {**reading.to_dict(), "checked_at": now.isoformat(timespec="minutes")}
        if reading.status == "READ":
            rec["judgement"] = judge_cpi(reading.value, prev, exp)
        store[event_key(e)] = rec
    return store


def main():
    from datetime import date

    import pravin_brief as P
    import pravin_config as PC
    from pravin_quant import IST
    cfg = PC.load()
    now = datetime.now(IST)
    events = P.load_events(ROOT / "events.csv", date.today() - __import__("datetime").timedelta(days=3), 10)
    store = check_released(events, now, cfg.get("results_sources", []))
    save(store)
    for k, v in store.items():
        print(k, v["status"], v.get("value"), v.get("reason") or v.get("judgement", {}).get("text", ""))


if __name__ == "__main__":
    main()
