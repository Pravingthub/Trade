from datetime import datetime, timezone

import pravin_marketread as MR

NOW = datetime(2026, 11, 11, 7, 0, tzinfo=timezone.utc)


def item(sid, name, chg, effect, stale=False, shock=False, unit="%"):
    return {"id": sid, "name": name, "change": chg, "effect": effect, "stale": stale, "shock": shock, "unit": unit}


GLOBAL = {"items": [item("SP500", "S&P 500", -0.38, "adverse"), item("NASDAQCOM", "Nasdaq", -0.74, "adverse"),
                    item("NIKKEI225", "Nikkei", 1.31, "supportive"), item("VIXCLS", "US VIX", -2.31, "supportive"),
                    item("DCOILBRENTEU", "Brent", -0.34, "supportive", stale=True)], "shocks": []}


def test_screenshot_day_reads_neutral_slightly_negative():
    r = MR.read(GLOBAL, [], [], NOW)
    assert r["lean"] == "neutral" and r["score"] == -1        # -2 -1 +1 +1
    assert "slightly negative" in r["how_to_use"] and "Brent" in " ".join(r["cautions"])
    assert r["drivers"][0]["text"].startswith("S&P 500")      # biggest weight first


def test_high_impact_news_and_shock_move_the_lean():
    news = [{"published_utc": "2026-11-11T05:00:00+00:00", "bias": "adverse", "impact": "high",
             "publisher": "RBI", "title": "MPC increases the policy repo rate"}]
    r = MR.read(GLOBAL, news, [], NOW)
    assert r["lean"] == "bearish" and r["score"] == -4
    assert "goes against it" in r["how_to_use"]
    g = {"items": [item("SP500", "S&P 500", -2.4, "adverse", shock=True)], "shocks": ["S&P 500 -2.40%"]}
    r2 = MR.read(g, [], [], NOW)
    assert r2["score"] == -4 and any("shock" in c for c in r2["cautions"])


def test_routine_and_old_news_ignored_and_events_become_cautions():
    news = [{"published_utc": "2026-11-09T05:00:00+00:00", "bias": "adverse", "impact": "high", "publisher": "x", "title": "old"},
            {"published_utc": "2026-11-11T05:00:00+00:00", "bias": "routine", "impact": "low", "publisher": "x", "title": "y"}]
    ev = [{"impact": "high", "event": "India CPI (after the close)", "affects_session": "2026-11-13", "today": False}]
    r = MR.read(None, news, ev, NOW)
    assert r["score"] == 0 and r["lean"] == "neutral"
    assert any("not fetched" in c for c in r["cautions"]) and any("India CPI affects" in c for c in r["cautions"])


def test_read_result_moves_the_lean_and_unread_release_is_flagged():
    ev_read = [{"impact": "high", "event": "India CPI (after the close)", "affects_session": "2026-11-13", "state": "read",
                "outcome": {"bias": "adverse", "text": "CPI 5.91% vs 5.54% last month: hotter → bad for markets ↓"}}]
    r = MR.read(None, [], ev_read, NOW)
    assert r["score"] == -3 and r["lean"] == "bearish" and r["drivers"][0]["source"] == "result"
    ev_unread = [{"impact": "high", "event": "India CPI (after the close)", "affects_session": "2026-11-13",
                  "state": "released_unread", "state_reason": "release not found in the configured sources yet"}]
    r2 = MR.read(None, [], ev_unread, NOW)
    assert r2["score"] == 0 and any("does NOT include it" in c for c in r2["cautions"])
