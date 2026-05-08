# Program Overview

This program is a 1DTE options trading planner. It does not place trades. It analyzes intraday data, selects a candidate option contract, applies risk controls, and outputs an ALLOWED or REJECTED plan. It logs every decision in an append-only event log for audit and replay.

## What it does (end-to-end)
1) Loads `config.json` for symbols, strategy rules, risk limits, and output settings.
2) Fetches intraday bars and an options chain via the configured data provider.
3) Runs the VWAP pullback signal engine with regime and time filters.
4) Scores nearby option contracts and selects the best candidate that passes liquidity gates.
5) Runs the risk engine (position sizing + hard limits + control-plane rules).
6) Emits a plan (ALLOWED or REJECTED).
7) Logs events to `events.jsonl` with UTC timestamps and correlation IDs.
8) Displays output in the CLI or web dashboard.

## Architecture
- Market data providers:
  - Yahoo with intraday bars + options chain.
  - Webull data API for intraday bars (no options chain yet).
  - Public API for options chain + quotes, with Yahoo bars fallback.
  - Optional fallback to Yahoo when a provider fails.
  - Hybrid mode: Webull for intraday bars + Yahoo for options chain.
- Signal engine: VWAP trend + pullback/reclaim (1m bars + 5m EMA trend).
- Higher-timeframe EMA filter (optional) to align with dominant trend.
- ATR calculations for volatility-aware sizing and stop notes.
- Sentiment regime filter (optional, advisory by default; manual or CNN Fear & Greed).
- Instrument service: options selection across chain, liquidity gates, scoring, greeks.
- Risk engine: sizing + caps + throttles + daily lockout + cooldown.
- Planner: combines signal, instrument selection, and risk decision into a plan.
- Journal: event log with UTC timestamps, run IDs, decision IDs, and config hash.

## Data providers
Configured in `config.json`:
- `data_provider`: `yahoo` or `webull`
- `data_provider_fallback`: if true, fallback to Yahoo on provider error

Notes:
- Yahoo data is delayed and not guaranteed for trading.
- Webull options chain is not available in the current API docs.
- Hybrid mode keeps Yahoo for options chain until Webull options data is available.
- Public API does not expose intraday bars, so Yahoo or Finnhub is used for bars.
- Public API uses personal access tokens created from a secret token.

## Signal rules (VWAP Pullback)
Long CALL:
- Price above VWAP on 1m
- VWAP slope positive
- 5m EMA fast > EMA slow
- Pullback near VWAP, then reclaim with momentum

Long PUT:
- Inverse conditions

Reject if:
- Too many VWAP crosses in the last `chop_lookback_minutes`
- Within the time filters near open or close (unless `scalp_mode` is true)
- Not enough bars to compute EMAs or pullback
- Stale bars (older than `data_quality.max_bar_age_minutes`)
- Higher-timeframe trend misaligned (if enabled)
- Sentiment advisory or block (if enabled)

## Option selection (contract scoring)
The program evaluates nearby strikes and scores each candidate after liquidity gates:

Liquidity gates (hard reject):
- Bid/ask missing or mid not available
- Quote age too old (`options.max_quote_age_minutes`)
- Spread too wide (`options.max_spread_pct`)
- Open interest below minimum
- Volume below minimum
- Price outside `[min_option_price, max_option_price]`
- For short DTE, missing IV (if `require_iv_for_short_dte` is true)
- IV deviation too large vs median within moneyness band (if configured)

Scoring (weighted):
- Spread score (tighter is better)
- Open interest score
- Volume score
- Delta proximity to target
- Price sanity score

Penalties:
- Missing IV/greeks
- Excessive gamma or theta
- Stale quote penalty

Top candidates and scores are logged to the event log for audit.

## Risk controls
Position sizing:
```
risk_per_contract = option_mid * 100 * premium_stop_pct
contracts = floor((equity * risk_pct) / risk_per_contract)
```
Then capped by max premium and max contracts.

Volatility adjustment (optional):
- Scales `risk_pct` toward a target ATR%.
- Supports ATR-based stop sizing via option delta.

Hard reject controls:
- Kill switch
- Data health gating
- Max notional per order
- Max contracts per order
- Max position per symbol (daily)
- Max total contracts per day
- Duplicate order window
- Throttle between signals
- Max trades per day
- Daily loss lockout
- Cooldown after loss

Daily loss and cooldown are enforced from the event log state (realized PnL is read from `events.jsonl`).

## Output
CLI (simple mode):
- Action (Buy CALL / Buy PUT / No trade)
- Strike and expiration

Web dashboard:
- Same as simple CLI output with auto-refresh and status panel.
- Shows cumulative session totals from the event log.
- Keeps accepted plans visible across refreshes and shows the latest underlying price.

## Event log (`events.jsonl`)
All decisions are logged as immutable events with UTC timestamps:
- signal
- instrument_selection
- plan
- reject_reason
- order_intent
- error
- reconnect
- provider_fallback

Each event includes:
- `event_id`, `event_time_utc`, `session_date_utc`
- `config_version` and `config_hash`
- `run_id` and `decision_id` for correlation

## How to run
CLI once:
```
py -m src
```

Watch mode:
```
py -m src --watch --interval 300
```

Web dashboard:
```
py -m src --web
```
Open `http://127.0.0.1:5500`.

## Key files
- `config.json`: all parameters
- `src/engine.py`: orchestration and logging
- `src/engines/signal_engine.py`: VWAP pullback logic
- `src/services/options_service.py`: chain scoring + greeks
- `src/risk.py`: sizing and risk controls
- `src/journal.py`: event logging
- `web/`: dashboard UI
- `SOP_CHECKLIST.md`: operating checklist for daily use

## Known limitations
- Yahoo data is delayed and not guaranteed for trading.
- Webull provider is not configured until API access is available.
- No live order execution; order intent only.

## Recommended next steps
- Wire a real-time options provider (Webull when approved).
- Add execution adapter + order state machine if you plan to trade live.
- Add a PnL updater if you want daily loss lockouts enforced with real trades.
