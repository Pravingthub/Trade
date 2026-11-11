"""Official announcements monitor (RBI, SEBI, BSE, and NSE once configured).

Reads the regulators' and exchanges' own RSS feeds, removes duplicates,
scores how much each item matters to an index trader with transparent rules,
and stores everything in news.db on your computer. The morning brief shows
what arrived since the last close; the live watcher can poll during the day.

Feed text is DATA. It is never treated as instructions, and the AI summary
only ever sees headlines that have passed through this module.

    python pravin_news.py fetch          # pull all enabled feeds
    python pravin_news.py latest         # what is new in the last 18 hours
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from pravin_quant import IST

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "news.db"
SOURCES = ROOT / "news_sources.csv"
UA = "PRAVIN-AI personal research (RSS reader)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, source TEXT, publisher TEXT, title TEXT, link TEXT,
    published_utc TEXT, ingested_utc TEXT, impact TEXT, score INTEGER, reasons TEXT, affects TEXT
);
CREATE TABLE IF NOT EXISTS fetches (source TEXT, at_utc TEXT, ok INTEGER, items INTEGER, error TEXT);
"""

# Transparent, editable rules. Score >= 6 is high, >= 3 medium, else low.
RULES = [
    (r"monetary policy|policy repo|repo rate|\bmpc\b|crr\b|slr\b|rate decision", 8, "monetary policy", "BANKNIFTY|NIFTY50"),
    (r"governor'?s statement|statement on developmental and regulatory", 6, "RBI governor statement", "BANKNIFTY|NIFTY50"),
    (r"liquidity|vrr\b|vrrr\b|variable rate (reverse )?repo|open market operation|\bomo\b", 3, "liquidity operation", "BANKNIFTY"),
    (r"\bf&o\b|derivative|futures|options|expiry|lot size|margin|position limit", 5, "derivatives rules", "NIFTY50|BANKNIFTY"),
    (r"trading hours|market timing|trading holiday|special (live )?trading session|muhurat", 6, "market timing", "NIFTY50|BANKNIFTY"),
    (r"inflation|cpi\b|wpi\b|gdp|index of industrial production|\biip\b|current account|forex reserves", 4, "macro data", "NIFTY50"),
    (r"foreign portfolio|\bfpi\b|\bfii\b|external commercial", 3, "foreign flows", "NIFTY50"),
    (r"circuit|surveillance|ban period|\basm\b|\bgsm\b", 3, "market surveillance", "NIFTY50"),
    (r"penalty|cancels certificate of registration|directions under section 35a|co-operative bank|nbfc", -4,
     "routine enforcement", ""),
    (r"auction of (state )?government securit|treasury bills|money market operations|sgb|sovereign gold", -3,
     "routine operations", ""),
]


@dataclass
class Item:
    source: str
    publisher: str
    title: str
    link: str
    published: datetime | None


def parse_date(s: str | None) -> datetime | None:
    """RSS dates, including RBI's which omit the timezone (taken as IST)."""
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%d-%b-%Y %H:%M:%S", "%d %b %Y %H:%M:%S"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if d is None:
            return None
    return d if d.tzinfo else d.replace(tzinfo=IST)


def parse_feed(xml_bytes: bytes, source: str, publisher: str) -> list[Item]:
    """RSS 2.0 and Atom. Malformed feeds raise ValueError."""
    try:
        root = ET.fromstring(xml_bytes.lstrip(b"\xef\xbb\xbf").strip())
    except ET.ParseError as e:
        raise ValueError(f"{source}: not valid XML ({e})") from e
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
        out.append(Item(source, publisher, title, (it.findtext("link") or "").strip(), parse_date(it.findtext("pubDate"))))
    for it in root.iter(f"{atom}entry"):
        link = it.find(f"{atom}link")
        out.append(Item(source, publisher, (it.findtext(f"{atom}title") or "").strip(),
                        link.get("href", "") if link is not None else "",
                        parse_date(it.findtext(f"{atom}updated") or it.findtext(f"{atom}published"))))
    return [i for i in out if i.title]


def score(title: str, default_affects: str) -> dict:
    t = title.lower()
    s, reasons, affects = 0, [], set(filter(None, default_affects.split("|")))
    for pat, pts, why, aff in RULES:
        if re.search(pat, t):
            s += pts
            reasons.append(why)
            affects |= set(filter(None, aff.split("|")))
    impact = "high" if s >= 6 else "medium" if s >= 3 else "low"
    return {"score": s, "impact": impact, "reasons": ", ".join(reasons) or "no rule matched", "affects": "|".join(sorted(affects))}


def item_id(i: Item) -> str:
    key = i.link or f"{i.publisher}|{i.title.lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def store(con, items: list[Item], default_affects: str, now: datetime | None = None) -> int:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    new = 0
    for i in items:
        sc = score(i.title, default_affects)
        cur = con.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (item_id(i), i.source, i.publisher, i.title, i.link,
                           i.published.astimezone(timezone.utc).isoformat() if i.published else None,
                           now.isoformat(), sc["impact"], sc["score"], sc["reasons"], sc["affects"]))
        new += cur.rowcount
    con.commit()
    return new


def read_sources(path: Path | None = None) -> list[dict]:
    p = path or SOURCES
    lines = [x for x in p.read_text().splitlines() if x.strip() and not x.startswith("#")]
    return [r for r in csv.DictReader(lines) if r["enabled"].strip() == "1" and r["url"].strip()]


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(5_000_000)


def fetch_all(con, sources: list[dict] | None = None, getter=http_get, now: datetime | None = None) -> list[dict]:
    """Fetch each feed independently; one failing source never stops the others."""
    now = now or datetime.now(timezone.utc)
    report = []
    for s in sources if sources is not None else read_sources():
        try:
            items = parse_feed(getter(s["url"]), s["id"], s["publisher"])
            n = store(con, items, s.get("default_affects", ""), now)
            con.execute("INSERT INTO fetches VALUES (?,?,?,?,?)", (s["id"], now.isoformat(), 1, n, None))
            report.append({"source": s["id"], "ok": True, "items": len(items), "new": n})
        except Exception as e:                       # network, HTTP 403, bad XML ...
            con.execute("INSERT INTO fetches VALUES (?,?,?,?,?)", (s["id"], now.isoformat(), 0, 0, str(e)[:200]))
            report.append({"source": s["id"], "ok": False, "error": str(e)[:200]})
    con.commit()
    return report


def latest(con, hours: float = 18, min_impact: str = "low", now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    order = {"low": 0, "medium": 1, "high": 2}
    rows = con.execute("SELECT * FROM news WHERE COALESCE(published_utc, ingested_utc) >= ? "
                       "ORDER BY score DESC, published_utc DESC", (since,)).fetchall()
    return [dict(r) for r in rows if order[r["impact"]] >= order[min_impact]]


def health(con, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = con.execute("SELECT source, MAX(at_utc) AS last, ok FROM fetches GROUP BY source").fetchall()
    last_ok = {r["source"]: r["last"] for r in con.execute("SELECT source, MAX(at_utc) AS last FROM fetches WHERE ok=1 GROUP BY source")}
    failing = [r["source"] for r in rows if not r["ok"]]
    stale = [s for s, t in last_ok.items() if now - datetime.fromisoformat(t) > timedelta(hours=24)]
    return {"sources": len(rows), "failing": failing, "stale": stale, "never_fetched": not rows}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    lt = sub.add_parser("latest")
    lt.add_argument("--hours", type=float, default=18)
    lt.add_argument("--min-impact", default="low", choices=["low", "medium", "high"])
    a = ap.parse_args(argv)
    con = connect()
    if a.cmd == "fetch":
        for r in fetch_all(con):
            print(f"{r['source']}: " + (f"{r['items']} items, {r['new']} new" if r["ok"] else f"FAILED ({r['error']})"))
    else:
        for r in latest(con, a.hours, a.min_impact):
            when = (r["published_utc"] or r["ingested_utc"])[:16]
            print(f"[{r['impact'].upper():6}] {when}Z {r['publisher']}: {r['title']}  ({r['reasons']}; affects {r['affects'] or '-'})")


if __name__ == "__main__":
    main()
