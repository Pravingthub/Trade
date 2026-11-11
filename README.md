# Vriddhi

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
| Trade journal | `pravin_journal.py` | Logs every paper or real trade with its plan; review shows late stop exits, off-plan trades, trades against the risk guard, overtrading days |
| Official announcements | `pravin_news.py` | RBI, SEBI and BSE feeds (NSE once you add the links), scored for impact, shown in the brief |
| Option maths | `pravin_options.py` | Implied volatility, Greeks, premium after time decay, break-even, liquidity checks, lot sizing for each strike |
| Today's plan | `pravin_plan.py` | One instruction per index at the top of the desk: what to do on each kind of open, trigger, stop, target, size, cancel and exit rules; paper-only until a setup proves itself |
| Stance engine | `pravin_stance.py` | Changes the stance during the day on rules (open, level breaks, high-impact news and heavyweight results, crude/gold/USD-INR/VIX moves, your risk limits), each change with its reason, capped to prevent flip-flopping |
| Stock screen | `pravin_screen.py` | Every evening: all NSE stocks → tradable ones (price and turnover filters) → the best 3–5 for tomorrow, each with triggers and a size that fits your risk; stock options only when one lot fits |
| Global markets | `pravin_global.py` | Overnight S&P 500, Nasdaq, Nikkei, US VIX, US 10-year, Brent, USD/INR from FRED (free, official) |
| Automation | `pravin_schedule.py` | Runs news, login reminder, brief, live alerts and the evening review on trading days (macOS) |
| API and dashboard | `pravin_api.py`, `build_dashboard.py` | Web API and the CPR dashboard page |

## Automatic morning refresh (GitHub)

`.github/workflows/desk.yml` rebuilds the public desk at 06:45 and 18:00 IST on weekdays with real global markets
(FRED) and official announcements. Prices stay DEMO there: your Zerodha data is used only on your own computer.
It needs one repository secret: **Settings → Secrets and variables → Actions → New repository secret**,
name `FRED_API_KEY`. Never put keys in any file in this repository; a test fails if one appears.

## Your settings

`vriddhi_config.json` holds capital (₹50,000–₹1,00,000), risk per trade, daily and weekly stops, max trades,
cool-off and loss lockout. Every tool reads it and refuses limits looser than the allowed ranges.
`events.csv`: mark each row `verified=yes` and update the `# reviewed:` date whenever you check the calendars;
an unchecked calendar keeps the desk at NO TRADE. See `AUDIT.md` for the latest audit and checklist.

## One-time setup (your Mac)

```bash
cd Trade                                   # your project folder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp env.example .env                        # then open .env and fill in your keys
pytest -q                                  # should say 276 passed
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

## Works today without Zerodha

```bash
python pravin_news.py fetch && python pravin_news.py latest     # official announcements
python pravin_brief.py                                          # brief on demo prices + real announcements
python pravin_journal.py open --instrument NIFTY --kind option --side long \
    --entry 120 --stop 95 --target 170 --qty 65 --setup discretionary --mode paper
python pravin_journal.py close 1 --exit 150 --reason manual
python pravin_journal.py review --days 30
python pravin_options.py --chain my_chain.csv --spot 25120 --expiry 2026-11-17 \
    --direction long --target 25220 --stop 25060 --minutes 90 --lot-size 65
```

The journal works for any trade from any broker. Log the plan **before** you enter.

## Every trading day

```bash
source .venv/bin/activate
python pravin_kite.py login                          # tokens expire every morning
python pravin_kite.py fetch --years 0.05             # top up recent candles
python pravin_brief.py --provider kite --telegram    # morning brief to your phone (add --ai for a summary)
python pravin_live.py --paper                        # live alerts, paper-trading each validated setup
python pravin_journal.py review --days 1             # after the close
```

Or automate everything except the Kite login (macOS):

```bash
python pravin_schedule.py install --provider kite
sudo pmset repeat wakeorpoweron MTWRF 07:40:00       # wake the Mac on weekday mornings
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
- Official announcements only (RBI, SEBI, exchanges). Commercial news and global market feeds need a licensed source, not chosen yet.
- Option maths uses Black-Scholes with constant volatility; IV jumps on fast days and around events. Treat values as estimates.
- No user accounts or database yet. This is a single-user tool that runs on your computer.

## Keep private

Your repo is public. Never commit `.env`, `.kite_token`, `journal.db`, `news.db`, or the `data/`, `results/`, `reports/` and `logs/` folders
(`.gitignore` blocks them). Do not publish a dashboard built from your Zerodha data to GitHub Pages:
Zerodha's data is for your personal use.

See `FORMULAS.md` for every formula and convention.
