# PRAVIN AI

Personal Indian-market analysis assistant built around CPR and pivot points.
**Analysis only: nothing in this codebase can place, modify or cancel an order.**
You decide and you execute in Kite.

## What it does

| Part | File | What you get |
|---|---|---|
| Engine | `pravin_quant.py` | CPR, floor and Camarilla pivots, indicators, risk controls, data checks |
| Zerodha data | `pravin_kite.py` | Daily login, 6+ years of NIFTY/BANKNIFTY candles stored on your computer, real NSE calendar |
| Backtester | `pravin_backtest.py` | Tests 4 CPR setups with Zerodha charges, slippage, out-of-sample and walk-forward checks |
| Morning brief | `pravin_brief.py` | Pre-market page: CPR/pivots for day, week and month, scenarios, events, validated setups, risk budget; optional AI summary and Telegram |
| Live alerts | `pravin_live.py` | Watches the market from Zerodha's live feed; alerts on level breaks and validated setups; pauses when data goes stale |
| API and dashboard | `pravin_api.py`, `build_dashboard.py` | Web API and the CPR dashboard page |

## One-time setup (your Mac)

```bash
cd Trade                                   # your project folder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp env.example .env                        # then open .env and fill in your keys
pytest -q                                  # should say 142 passed
```

1. **Kite Connect**: create an app at developers.kite.trade (paid "Connect" plan; check the current price there).
   Put the API key and secret in `.env`. Set the app's redirect URL to `http://127.0.0.1` (any URL works; you only copy a value from it).
2. **Telegram (optional)**: create a bot with @BotFather, send it any message, then put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.
3. **Download history** (takes a few minutes, once):
   ```bash
   python pravin_kite.py login
   python pravin_kite.py fetch --years 6
   ```
4. **Backtest your setups** (re-run monthly):
   ```bash
   python pravin_backtest.py --id NIFTY50
   python pravin_backtest.py --id BANKNIFTY
   ```
   Results go to `results/`. Only setups marked `candidate_for_paper_trading` are allowed to send live setup alerts.

## Every trading day

```bash
source .venv/bin/activate
python pravin_kite.py login                          # tokens expire every morning
python pravin_kite.py fetch --years 0.05             # top up recent candles
python pravin_brief.py --provider kite --telegram    # morning brief to your phone (add --ai for a summary)
python pravin_live.py                                # live alerts 09:15-15:30; leave it running
```

Keep `events.csv` up to date (RBI, Fed, budget, results) and, if you like,
`global_markets.csv` with overnight closes. The brief tells you which inputs are missing.

## Rules the code enforces

- Levels use only completed sessions; muhurat sessions never become "yesterday".
- Backtests: next-bar entries, stop assumed before target inside a bar, exit by 15:15, dated Zerodha charges (STT changed 1 Oct 2024 and 1 Apr 2026), 60/25/15 split with a hidden holdout, walk-forward, slippage at 0-3x. On a purely random market no setup is allowed to pass.
- A setup that passes is only a **candidate for paper trading**, never "approved".
- Live alerts: one alert per event, risk guard shown on every setup (3 trades/day, cooldown after a loss, two losses end the day, daily and weekly loss stops), lot sizing that says NO TRADE rather than rounding up, all setups paused when the feed is stale.
- The AI summary may only restate numbers already in the brief; anything else is rejected and the brief goes out without it.

## Limits to know

- Index candles have no volume, so VWAP-based setups are not tested. Expired option contracts have no history at Zerodha, so option premiums cannot be backtested; trades are simulated on the index with futures charges.
- Lot size is today's value from Kite; historical lot sizes differed (R-multiples are unaffected).
- News and global market feeds are not connected: no licensed source has been chosen yet.
- No user accounts or database yet. This is a single-user tool that runs on your computer.

## Keep private

Your repo is public. Never commit `.env`, `.kite_token`, or the `data/`, `results/` and `reports/` folders
(`.gitignore` blocks them). Do not publish a dashboard built from your Zerodha data to GitHub Pages:
Zerodha's data is for your personal use.

See `FORMULAS.md` for every formula and convention.
