# Vriddhi — Technical audit and enhancement sprint (11 Nov 2026)

Branding: kept **Vriddhi** (chosen by the user on 11 Nov 2026), not reverted to PRAVIN AI.
Tests after this sprint: **245 passed, 0 failed** (`pytest -q`); lint clean (`ruff check .`).

## 1. Audit findings

| Area | Finding |
|---|---|
| Architecture | Flat Python modules: engine (`pravin_quant`), API + demo provider (`pravin_api`), Zerodha data (`pravin_kite`), backtester, brief, live watcher, journal, news, options, regime, candidates, confluence (`pravin_watch`), scheduler, config; static desk built by `build_dashboard.py`. |
| Data sources | DEMO fixture provider (simulated). Zerodha historical/live adapter written and tested with a fake client only; never run against the real API (no account yet). RBI/SEBI/BSE feeds implemented; RBI verified with real items. No global-market or commercial news source. |
| CPR / pivots | Hand-calculated fixtures (3 cases) for CPR, floor, Camarilla; weekly/monthly aggregation over exchange sessions with completeness checks and a no-look-ahead test. |
| Indicators | EMA, RSI, ATR, ADX/DMI, Bollinger, OBV, MFI equal TA-Lib to 1e-8; MACD converges after warm-up (documented TA-Lib seeding difference); VWAP hand-tested. |
| Historical data | Chunked within Kite limits; calendar from real index candles plus NSE holiday file; Muhurat excluded. Untested against real Kite responses. |
| Backtesting | Next-bar entries, stop-first ambiguity, dated STT schedule, 60/25/15 split with hidden holdout, walk-forward, slippage 0–3×, random-walk sanity test. Index price with futures charges (expired option history unavailable at Kite). |
| Options | Black-Scholes European, IV by bisection, Greeks verified by finite differences and textbook values. |
| Risk | RiskPolicy, DailyGuard, journal-rebuilt guard, position sizing with whole lots. |
| News / events | Official RSS only, rule-based impact/bias. Events from a hand-maintained CSV. |
| Security | Secrets only in local `.env`; token file chmod 600; `.gitignore` covers secrets, data, journal, news, logs. Public GitHub repo: only demo data published. |

## 2. Critical bugs and inconsistencies found (all fixed)

| # | Bug | Severity | Fix |
|---|---|---|---|
| 1 | Example rows in `events.csv` counted as real events; calendar shown "ok" just because the file had rows | High | `verified` column, `# reviewed:` date; states not_checked / stale / checked |
| 2 | Live watcher never flagged a feed that produced no tick at all | High | Staleness measured from the 09:15 open when no tick has arrived |
| 3 | Synthetic option chain could return status "ok" with lot sizes | High | Synthetic → status "illustration", never eligible, no size |
| 4 | Lot sizes hardcoded (65/30) | High | Config + Kite instrument master; "unverified" blocks sizing |
| 5 | BANKNIFTY given a weekly Tuesday expiry | Medium | Expiry rules per contract (monthly last Tuesday), holiday-shifted, labelled as assumptions |
| 6 | Option reward/risk ignored charges and volatility change | Medium | Net of round-trip charges; worst of IV unchanged / IV −2 pts |
| 7 | Paper trades opened even when the risk guard said stop | Medium | Paper trades blocked (`GuardBlocked`); live trades recorded and flagged |
| 8 | Guard counted trades whose close time was in the future | Low | Only closes at or before "now" count |
| 9 | Four numbers computed in the browser (trades left, weekly stop, break-even points, ATR distance) | Medium | All computed in Python; a test bans them from the page |
| 10 | API port exposed on all network interfaces via Docker | Medium | Compose binds to 127.0.0.1 only |
| 11 | Capital fixed at ₹1,00,000 in several places | Medium | `vriddhi_config.json`, validated range ₹50,000–₹1,00,000 |
| 12 | News rule missed "cuts"/"hikes" | Low | Fixed and tested |

## 3. Checklist

### P0 — Data integrity
- [x] Provenance model LIVE_VERIFIED / DELAYED / HISTORICAL / DEMO / UNAVAILABLE (`provenance_status`); a stale live feed drops to DELAYED
- [x] Source and timestamp on every desk panel and every candidate (provider, data-to date, build time, news fetch time, calendar review date)
- [x] Actionable output blocked for DEMO/HISTORICAL/DELAYED data, unhealthy data, unchecked calendar, synthetic options
- [x] Empty calendar distinguished from checked-with-no-events
- [x] Market-data health HEALTHY / DEGRADED / BLOCKED with reasons
- [x] No calculations in the page (enforced by test)

### P0 — Market regime
- [x] Four layers: short-term (5m/15m), intraday (1h), swing (daily), higher (weekly/monthly)
- [x] Deterministic documented rules (`pravin_regime.py`, FORMULAS.md)
- [x] Disagreements explained in words

### P0 — Options analytics
- [x] Synthetic vs live chain explicit; synthetic never actionable
- [x] Validation: expiry date and trading day, lot size, strike multiples, bid/ask, spot inside chain, chain age
- [x] Model and Greeks documented
- [x] Premium at entry, target and stop after time decay, with IV unchanged and ±2 points
- [x] Spread and round-trip charges in reward/risk and break-even

### P1 — CPR and support/resistance
- [x] Daily/weekly/monthly verified independently (fixtures + aggregation tests)
- [x] Completed exchange-period aggregation (holiday weeks, missing sessions refused)
- [x] Raw TC/BC and normalised boundaries displayed
- [x] Distance to nearest zone edge and midpoint, in points and ATR
- [x] CPR statistics with sample sizes, quartiles, % up, next-day returns
- [x] Daily/weekly/monthly confluence zones

### P1 — Risk management
- [x] Capital configurable, validated to ₹50,000–₹1,00,000
- [x] Risk per trade, daily/weekly stop, max trades, cooldown, loss lockout configurable and validated
- [x] Limits enforced in backend (paper trades blocked; candidates NO_TRADE)
- [x] AI cannot bypass: AI writes narrative only; verdicts and candidates never read it; invented numbers rejected
- [x] Brokerage, STT, GST, exchange, SEBI, stamp duty, slippage in backtests, journal and options
- [x] Explicit NO TRADE state

### Recommendation engine
- [x] Candidate record with all 16 required fields (`pravin_candidates.py`)
- [x] No score or probability anywhere
- [x] Decisions NO_TRADE / WATCH_ONLY / PAPER_TRADE; no live-trading decision exists yet
- [ ] Live watcher alerts still use their text format; switching them to candidate records is the next step

### Additional modules
- [x] Designs and estimates in `ADDITIONAL_MODULES.md` (not implemented, as requested)

### Implementation discipline
- [x] No order placement anywhere (tests check source files)
- [x] No paid services added; no scraping; no fabricated data (demo labelled everywhere; news is real RBI items)
- [x] Unit and integration tests added (41 new, 245 total)

## 4. Dependencies and costs
No new dependencies, no paid services. Cost so far ₹0. Kite Connect (₹500 per 30 days) only after a Zerodha account exists.

## 5. Known limitations
- Every price is DEMO until the Zerodha account and Kite Connect exist; the Kite adapter has only been tested against a fake client.
- Expiry rules and lot sizes are assumptions until the Kite instrument master is downloaded.
- Regime and CPR-width thresholds are research defaults, not validated edges.
- Option model assumes constant volatility within each scenario; real IV can move more than ±2 points around events.
- Event calendar is maintained by hand; no licensed calendar feed.
- Index candles have no volume; VWAP comes from futures (current contract only).

## 6. Order of the next work
1. Zerodha account → Kite Connect → verify CPR against Kite charts to the paisa.
2. Backtest the four setups plus your own written-down setups.
3. Switch live alerts to candidate records; wire the live option chain into the strike check.
4. Paper trading 6–8 weeks; review against backtest.
5. Additional modules in the order in `ADDITIONAL_MODULES.md`.
