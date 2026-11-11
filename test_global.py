import json
from datetime import date

import pravin_global as G


def fred(values_by_series):
    """Fake FRED: {sid: [(date, value), ...newest first]}"""
    def getter(url):
        sid = url.split("series_id=")[1].split("&")[0]
        if sid not in values_by_series:
            raise OSError("HTTP 400")
        obs = [{"date": d, "value": v} for d, v in values_by_series[sid]]
        return json.dumps({"observations": obs}).encode()
    return getter


TODAY = date(2026, 11, 11)     # Wednesday IST -> last US session Tuesday 10 Nov
BASE = {sid: [("2026-11-10", "100"), ("2026-11-09", "100")] for sid in G.SERIES}


def test_skips_fred_placeholders():
    g = fred({"SP500": [("2026-11-11", "."), ("2026-11-10", "6900"), ("2026-11-09", "6850")]})
    obs = G.fetch_series("SP500", "k", g)
    assert obs[0] == (date(2026, 11, 10), 6900.0)


def test_risk_off_and_shock():
    v = dict(BASE)
    v["SP500"] = [("2026-11-10", "98"), ("2026-11-09", "100")]        # -2% : shock, adverse
    v["VIXCLS"] = [("2026-11-10", "24"), ("2026-11-09", "20")]        # +20% : shock, adverse
    v["DCOILBRENTEU"] = [("2026-11-10", "101"), ("2026-11-09", "100")]  # +1% adverse
    s = G.fetch_all("k", TODAY, fred(v))
    assert s["tone"] == "shock" and len(s["shocks"]) == 2
    assert s["adverse"] == 3 and s["supportive"] == 0


def test_yield_change_in_points_and_risk_on():
    v = dict(BASE)
    v["DGS10"] = [("2026-11-10", "4.10"), ("2026-11-09", "4.20")]     # -0.10 pts: supportive
    v["SP500"] = [("2026-11-10", "101"), ("2026-11-09", "100")]
    v["NIKKEI225"] = [("2026-11-10", "101"), ("2026-11-09", "100")]
    s = G.fetch_all("k", TODAY, fred(v))
    y = next(i for i in s["items"] if i["id"] == "DGS10")
    assert y["unit"] == "pts" and abs(y["change"] + 0.10) < 1e-9 and y["effect"] == "supportive"
    assert s["tone"] == "risk-on"


def test_stale_values_do_not_vote_and_failures_are_reported():
    v = {k: x for k, x in BASE.items() if k != "NASDAQCOM"}
    v["SP500"] = [("2026-10-30", "90"), ("2026-10-29", "100")]        # old: ignored
    s = G.fetch_all("k", TODAY, fred(v))
    sp = next(i for i in s["items"] if i["id"] == "SP500")
    assert sp["stale"] and not sp["shock"] and s["tone"] == "mixed"
    assert "NASDAQCOM" in s["errors"]


def test_key_from_environment_is_picked_up(monkeypatch, tmp_path):
    import pravin_kite as K
    monkeypatch.setenv("FRED_API_KEY", "abc")
    assert K.load_env(tmp_path / "none.env")["FRED_API_KEY"] == "abc"


def test_desk_shows_global_panel(monkeypatch, tmp_path):
    import json as _json

    import build_dashboard as D
    v = dict(BASE)
    s = G.fetch_all("k", TODAY, fred(v))
    cache = tmp_path / "global.json"
    G.save(s, cache)
    monkeypatch.setattr(G, "CACHE", cache)
    monkeypatch.setattr(G.load, "__defaults__", (cache,))
    out = tmp_path / "d.html"
    D.build(out, "fixture", news_db=tmp_path / "n.db", journal_db=tmp_path / "j.db")
    d = _json.loads(out.read_text().split("const DATA = ")[1].split(";\n")[0])
    assert d["global"]["items"] and "globalPanel" in out.read_text()


def test_no_api_key_value_in_any_repository_file():
    import pathlib
    import re
    pat = re.compile(r"(FRED|KITE|OPENAI|TELEGRAM)_[A-Z_]*(KEY|SECRET|TOKEN)[ \t]*[=:][ \t]*['\"]?[0-9A-Za-z:_-]{16,}")
    root = pathlib.Path(G.__file__).parent
    for p in root.glob("*"):
        if p.is_file() and p.suffix in (".py", ".json", ".csv", ".md", ".yml", ".html", ".txt", ".example"):
            assert not pat.search(p.read_text(errors="ignore")), p.name
