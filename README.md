# PRAVIN AI

Personal Indian-market analysis assistant built around CPR and pivot points.
**Analysis only: there is no order placement anywhere in this codebase.**

Status: Milestones 1 and 2 (foundation and quantitative engine). All data is
**simulated demo data** until the Zerodha adapter (Milestone 5) is enabled.

## Files

| File | What it is |
|---|---|
| `pravin_quant.py` | The engine: CPR, pivots, periods, indicators, risk controls, data quality |
| `pravin_api.py` | FastAPI app, data-provider interface, DEMO data provider |
| `test_*.py`, `conftest.py`, `levels_reference.json` | 82 tests; hand-calculated reference values |
| `build_dashboard.py`, `dashboard_template.html` | Builds the CPR & Pivots preview page |
| `FORMULAS.md` | Every formula and convention used |
| `requirements*.txt`, `pyproject.toml` | Dependencies, test and lint settings |
| `Dockerfile`, `docker-compose.yml` | Container setup |
| `env.example` | Settings template (no real keys) |
| `.gitignore`, `.github/workflows/ci.yml` | Created on GitHub; see setup notes |

## Run locally

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest -q                      # 82 tests
ruff check .                   # lint
uvicorn pravin_api:app --reload        # API docs at http://localhost:8000/docs
python build_dashboard.py              # writes dashboard.html
```

TA-Lib is used **only in tests**, as an independent reference for the
indicator code. If it fails to install, those tests skip and everything else runs.

## Key endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/analytics/cpr/{id}?period=day\|week\|month&as_of=YYYY-MM-DD` | CPR with source candle, width percentile, relation to previous |
| `GET /api/v1/analytics/pivots/{id}?period=...&camarilla_multiplier=1.1` | Floor and Camarilla pivots |
| `GET /api/v1/analytics/cpr-history/{id}?sessions=40` | Daily CPR history with virgin flags |
| `GET /api/v1/analytics/indicators/{id}?interval=day\|15minute\|60minute` | Indicators and regime |
| `GET /api/v1/analytics/session-levels/{id}` | Previous session high/low, opening range, VWAP |
| `GET /api/v1/analytics/multi-timeframe/{id}` | 15m/1h/day/week/month alignment (completed bars only) |
| `GET /api/v1/risk/position-size?capital=&entry=&stop=&target=&lot_size=` | Lot-aware sizing with NO TRADE outcomes |
| `GET /api/v1/data-health` | Validation, missing sessions, freshness |

Demo instruments: `NIFTY50`, `BANKNIFTY`, `NIFTYFUT`, `RELIANCE`.

## Status of Milestones 1 & 2

82 tests pass; lint clean. CPR, floor and Camarilla match hand calculations;
EMA, RSI, ATR, ADX/DMI, MACD, Bollinger, OBV and MFI match TA-Lib to
floating-point precision. Limitations: prices are simulated, the holiday
calendar is a demo (load the official NSE list before real data), and the
narrow/wide and regime thresholds are unvalidated research defaults.
Next: Milestone 3 (Supabase, auth, row-level security). Cost so far: ₹0.

## Never commit
A real `.env`, API keys, Kite secrets or tokens, Supabase service-role keys, personal data.
