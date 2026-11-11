# Additional analytical modules — designs and estimates

Not implemented. Each needs a real data source first; none may display values it cannot source.

| Module | Design | Data source | Cost | Effort |
|---|---|---|---|---|
| India VIX and volatility regime | VIX level, 1-year percentile, change vs 5-day average; regime low/normal/high/extreme; feeds option IV-shock size | Kite: INDIA VIX index candles (historical + live) | Included in Kite Connect | 0.5 day |
| Option IV percentile and term structure | ATM IV per expiry from live chain; IV rank/percentile over 1 year (needs our own daily snapshots, since expired option history is unavailable); near vs next expiry slope | Kite live quotes; self-stored daily snapshots | Included | 2 days + 1 year of snapshots before percentiles mean anything |
| Market breadth and sector strength | Advance/decline of NIFTY 50 constituents, % above 20/50-day EMA, sector index relative strength vs NIFTY | Kite candles for constituents and sector indices | Included (more API calls) | 2 days |
| Volume Profile (POC, VAH, VAL, HVN, LVN) | Session and composite profiles from futures 1-minute volume; 70% value area; nodes by peaks/troughs | Kite NIFTY/BANKNIFTY futures minute candles (current contract only) | Included | 2 days; history limited to the live contract's life |
| Anchored VWAP | Already in the engine; needs futures volume and anchors (swing high/low, event days) in the UI | Kite futures candles | Included | 0.5 day |
| Global market correlation | Rolling 60-day correlation of NIFTY with S&P 500, Nikkei, USD/INR, crude | Needs a licensed global data source | Unknown; requires approval | 1 day after a source is chosen |
| Institutional positioning | FII/DII cash and F&O participant-wise OI | NSE publishes daily reports; check NSE terms of use before automated download | Free if terms allow | 1–2 days |
| Strategy performance analytics | Paper and live results per setup vs backtest expectation; drift alarms when live expectancy falls outside the backtest's confidence band | Our own journal | None | 1.5 days |

Explicitly out of scope: claims about institutional order flow, dark-pool or "smart money" activity. No source we can lawfully use provides it.
