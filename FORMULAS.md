# Formulas and conventions

Every number PRAVIN AI shows comes from these definitions. Tests: `tests/unit`.

## Source period
Levels for trading date D use the previous **completed** period only:
- Daily: the last exchange session before D. If that session is missing from the data, the engine refuses (`IncompletePeriod`) instead of silently using an older day.
- Weekly: the last ISO exchange week (Mon–Sun) that ended before D's week, built from the sessions that actually traded. A holiday week with 4 sessions is complete; a week missing a session the calendar expects is refused.
- Monthly: the last calendar month before D's month, same completeness rule.
- Data on or after D is never read (tested by poisoning future candles).

## CPR
P = (H + L + C) / 3, BC = (H + L) / 2, TC = 2P − BC.
Top = max(TC, BC), Bottom = min(TC, BC). Raw TC/BC are kept; when the close is in the lower half of the range, raw TC sits below raw BC.
Width = Top − Bottom; Width % = Width / P × 100.

Narrow/wide: percentile of today's width % among this instrument's own last 250 daily (104 weekly, 36 monthly) CPR widths. ≤ 20th = narrow, ≥ 80th = wide. These thresholds are **research defaults to be validated by backtesting**, not proven edges.

Relation to previous CPR: higher value (entirely above), lower value (entirely below), overlapping higher/lower, inside, outside, unchanged.
Virgin CPR: the session's range never touched [Bottom, Top].

## Floor pivots
R1 = 2P − L, S1 = 2P − H, R2 = P + (H − L), S2 = P − (H − L), R3 = H + 2(P − L), S3 = L − 2(H − P).

## Camarilla
Range = H − L, k = Range × m (m = 1.1 by default, configurable).
R1..R4 = C + k/12, k/6, k/4, k/2. S1..S4 = C − the same.

## Indicators
Convention: EMA and Wilder smoothing are seeded with a simple average of the first n values (same as TA-Lib and TradingView-style charts such as Kite's). Values before warm-up are blank, never extrapolated.

| Indicator | Definition | First value | Parity |
|---|---|---|---|
| EMA n | α = 2/(n+1), SMA seed | bar n−1 | TA-Lib exact |
| RSI 14 | Wilder averages of gains/losses | bar 14 | TA-Lib exact |
| ATR 14 | Wilder average of true range; first bar has no TR | bar 14 | TA-Lib exact |
| +DI/−DI/ADX 14 | Wilder sums of DM and TR; ADX = Wilder avg of DX | DI bar 14, ADX bar 27 | TA-Lib exact |
| MACD 12/26/9 | EMA12 − EMA26 (each seeded independently), signal EMA9 | bar 25 / 33 | TA-Lib after warm-up (TA-Lib aligns seeds) |
| Bollinger 20, 2 | SMA ± 2 × population std-dev | bar 19 | TA-Lib exact |
| OBV | cumulative ± volume, starts at first volume | bar 0 | TA-Lib exact |
| MFI 14 | typical-price money flow ratio | bar 14 | TA-Lib exact |
| VWAP | Σ(TP × V) / ΣV, TP = (H+L+C)/3, reset each IST session | first bar | hand-calculated test |
| Relative volume | V / mean of previous 20 bars | bar 20 | hand test |

"Exact" = equal within 1e-8 relative tolerance across random, trending, gappy and quiet test series.

**Index volume:** NIFTY 50 and NIFTY BANK have no traded volume. The engine returns VWAP and volume indicators as unavailable for an index and computes index VWAP from the configured future instead. It never fabricates index volume.

## Intraday resampling
15-minute and 1-hour bars are anchored to 09:15 IST and never cross a session boundary, so the last hourly bar of a day is 15:15–15:30 (as on Kite).

## Risk controls (defaults, configurable)
- Size = largest whole number of lots with loss-at-stop ≤ 1% of capital. If one lot exceeds the budget, size is zero (NO TRADE), never rounded up.
- Minimum reward/risk 1:2.
- Max 3 new trades per day; 30-minute cooldown after a loss; two consecutive losses end the day; 2% daily and 5% weekly loss stops.
- Stop discipline report: count and rupee cost of exits beyond the planned stop.
