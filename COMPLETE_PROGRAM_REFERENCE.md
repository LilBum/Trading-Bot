# Complete Program Reference

This document explains every aspect of the 1DTE options trading planner: architecture, data flow, configuration, checks, outputs, and logging.

## Purpose
The program is a decision pipeline. It does not place trades. It reads market data, evaluates a setup, selects the best contract, applies risk controls, and outputs an ALLOWED or REJECTED plan.

## High-level architecture
- MarketDataProvider: pulls intraday bars and options chains.
- SignalEngine: evaluates VWAP pullback setup and regime filters.
- InstrumentService: applies liquidity gates and scores contracts across strikes.
- RiskEngine: sizes positions and enforces hard limits.
- Planner: merges all gates into one plan.
- EventJournal: immutable event log for audit/replay.
- Web dashboard and CLI output.

## Execution flow (end-to-end)
1) Load `config.json`.
2) Select market data provider (Yahoo by default, Webull stub optional).
3) For each symbol:
   - Fetch intraday bars.
   - Fetch options chain.
   - Evaluate signal rules.
   - Select best contract from chain.
   - Run risk controls and sizing.
   - Emit ALLOWED/REJECTED plan.
4) Log events to `events.jsonl`.
5) Display output in CLI or web dashboard.

## Market data providers
Configured in `config.json`:
- `data_provider`: `yahoo`, `webull`, `hybrid` (Webull bars + Yahoo options chain), or `public`.
- `data_provider_fallback`: if true, fallback to Yahoo (disabled in live mode).

Webull config (hybrid/webull):
- `webull.app_key` / `webull.app_secret` or env `WEBULL_APP_KEY` / `WEBULL_APP_SECRET`.
- `webull.region`: API region (default `us`).
- `webull.api_endpoint`: optional override.

Public config:
- `public.secret_token` or env `PUBLIC_SECRET_TOKEN`.
- `public.validity_minutes`: access token validity for API calls.
- `public.account_id`: optional; discovered automatically if omitted.
- `public.base_url`: API base (default `https://api.public.com`).
- `public.bars_source`: `yahoo` or `finnhub` (Finnhub provides intraday bars).
- `public.use_greeks`: enables option greeks fetch (needed for IV).
- `public.max_greeks`: max option symbols per greeks request (Public limit 250).

Finnhub config:
- `finnhub.api_key` or env `FINNHUB_API_KEY`.
- `finnhub.base_url`: API base (default `https://finnhub.io`).

Live mode behavior:
- If `mode` is `live` and provider is not Webull, the program fails closed and logs `provider_blocked`.

## Signal engine (VWAP pullback)
Setup logic:
- Uses 1m bars for VWAP and pullback.
- Uses 5m EMAs for trend.
- Optional higher-timeframe EMA filter (default 15m) to align with dominant trend.
- Optional sentiment filter (manual or CNN Fear & Greed) in advisory mode by default.

CALL setup:
- Price above VWAP
- VWAP slope positive
- 5m EMA fast > EMA slow
- Pullback near VWAP then reclaim with momentum

PUT setup:
- Inverse conditions

Reject reasons:
- Bar is stale (`data_quality.max_bar_age_minutes`)
- Not enough 5m bars for EMAs
- Not enough higher-timeframe bars (if enabled)
- Higher-timeframe trend misaligned (if enabled)
- Sentiment advisory or block (configurable) (if enabled)
- Too many VWAP crosses (chop)
- Time filter blocks (open/close)
- Not enough 1m bars for pullback
- No setup conditions

## Instrument selection (options chain)
Contract selection flow:
1) Filter chain by direction (CALL/PUT).
2) Apply moneyness preference (ATM or ITM-only).
3) Limit candidates to `max_candidates` near ATM.
4) Apply hard liquidity gates.
5) Score remaining candidates and pick best.

Hard liquidity gates:
- Missing bid/ask
- Mid price invalid
- Mid outside bid/ask
- Quote time missing (if `require_quote_time` true)
- Quote too old (`max_quote_age_minutes`)
- Spread too wide (`max_spread_pct`)
- Open interest too low
- Volume too low
- Price outside bounds (`min_option_price`, `max_option_price`)
- Missing IV for short DTE (if required)
- IV deviation too large vs median within moneyness band (if configured)

Scoring inputs:
- Spread (tighter is better)
- Open interest
- Volume
- Delta proximity to target
- Price sanity (mid range)

Penalties:
- Missing IV
- High gamma or theta
- Stale quotes

Top candidates are logged with scores and quote age.

## Risk engine
Position sizing:
```
risk_per_contract = option_mid * 100 * premium_stop_pct
contracts = floor((equity * risk_pct) / risk_per_contract)
```
Then capped by:
- `max_premium_per_trade`
- `max_contracts`

Volatility-adjusted risk (optional):
- Uses ATR% to scale `risk_pct` toward a target volatility level.
- Supports ATR-based stop sizing using delta and underlying ATR (`stop_mode: delta_atr`).
- Logs base vs adjusted risk pct with ATR target for audit.

Hard rejects:
- Kill switch
- Data provider unhealthy (if `block_on_data_error` true)
- Max notional per order
- Max contracts per order
- Max position per symbol
- Max total contracts per day
- Duplicate order window
- Signal throttle
- Max trades per day
- Daily loss lockout
- Cooldown after loss
- Mid vs last price deviation (if configured)

Daily lockout and cooldown:
- Reads `events.jsonl` daily state.
- Only uses `pnl_update` or `fill` events when `logging.pnl_strict` is true.
- Applies max daily loss and cooldown time.

## Data health score
Each plan computes a data health score:
- Starts at 1.0
- Deducts penalties for stale bars, stale quotes, or missing IV.
- Rejects plan if below `data_quality.min_score`.

## Outputs
CLI output:
- Simple mode shows Action, Strike, Expiration.
- Detailed mode shows full plan, sizing, and reject reasons.

Web dashboard:
- Cards with Action, Strike, Expiration, and data health score.
- Summary panel (Total, Allowed, Rejected, Avg Data Health).
- Session totals (cumulative allowed/rejected for the exchange day).
- Accepts persist in the UI across refreshes for the session day.
- Cards show the latest underlying price snapshot.
- Top reject reasons.

Web API:
- `GET /api/plans` returns the latest plans, last update time, and any error text.
- Response includes `session_totals` and `session_accepts` for the exchange day.

## Event logging
All events are append-only JSONL in `events.jsonl`:
- signal
- instrument_selection
- plan
- reject_reason
- order_intent
- error
- reconnect
- provider_fallback
- provider_blocked
- pnl_update

Each event includes:
- event_id
- event_time_utc
- session_date_utc
- session_date_exchange
- config_hash, config_version
- run_id, decision_id

Plan payload fields:
- symbol, timestamp, status, setup, direction, entry_trigger, invalidation
- premium_stop, targets, contracts, estimated_risk, estimated_premium
- option_contract (strike, expiration, bid/ask/mid, IV, greeks, quote time)
- reject_reasons, warnings, regime_info
- data_health_score, atr_value, atr_pct, risk_pct_base, risk_pct_used, atr_target_pct

Order intent payload additions:
- arrival_mid, arrival_spread_pct, quote_time_utc
- next_bar_mid, next_bar_time_utc (placeholders until broker-grade quotes are available)

## Key files
- `config.json`: all parameters
- `src/engine.py`: orchestration + logging
- `src/engines/signal_engine.py`: VWAP pullback logic
- `src/services/options_service.py`: chain scoring + greeks
- `src/risk.py`: sizing + controls
- `src/journal.py`: event log writer
- `src/state.py`: daily PnL state reader
- `web/`: dashboard UI
- `SOP_CHECKLIST.md`: operating checklist for daily use

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

Record PnL update (for daily lockout):
```
py -m src --record-pnl -125.50
```

## CLI arguments
- `--watch`: loop continuously at the configured interval.
- `--interval`: seconds between runs (watch or web refresh).
- `--web`: start the local dashboard server.
- `--host`: bind host for the dashboard server.
- `--port`: bind port for the dashboard server.
- `--record-pnl`: append a realized PnL event (used by daily loss lockout).
- `--webull-mode`: choose Webull utility mode (`paper` or `live`) for account/order/trade-history commands.
- `--analyze-webull-trades`: fetch paginated Webull order history and print analytics.
- `--trade-start-date`: optional history start (`YYYY-MM-DD`).
- `--trade-end-date`: optional history end (`YYYY-MM-DD`).
- `--trade-page-size`: page size for history fetch.
- `--trade-max-pages`: maximum pages to fetch.
- `--trade-report-path`: optional JSON output path containing full history + analysis.

## Config reference (high level)
- `config.json` accepts `//`, `/* */`, and `#` comments (parsed before JSON load).
- `mode`: `paper` or `live` (live fails closed without broker-grade data).
- `data_provider`: `yahoo`, `webull`, or `hybrid`; `data_provider_fallback` for paper only.
- `account`: equity, risk %, daily loss %, max trades/day, cooldown.
- `position_sizing`: premium stop %, max premium, max contracts, ATR sizing options.
- `strategy`: symbols, bar intervals, VWAP/EMA/pullback thresholds, time filters, ATR, higher-timeframe filter.
- `sentiment`: optional regime filter (manual or CNN Fear & Greed), advisory by default (`mode: advisory|hard_block`).
- `data_quality`: max bar age, min score, penalties for stale or missing fields.
- `options`: DTE target, liquidity gates, quote age, IV/Greek requirements, scoring.
- `options.iv_deviation_band_pct`: moneyness band used for IV anomaly checks.
- `risk_controls`: kill switch, max notional, caps, throttles, duplicate window.
- `logging`: journal paths, strict PnL parsing.
- `output`: `simple` or `detailed`.
- `watch` / `web`: default behavior and intervals.

## Tests
- `pytest` runs the suite in `tests/`.
- Risk tests cover kill switch, throttles, caps, daily loss lockout, cooldown.
- Options tests cover quote age, missing IV hard reject, IV deviation reject.

## Limitations
- Yahoo data is delayed and not intended for trading.
- Webull provider is stubbed until API access is available.
- No live execution; only order intent is logged.

## Next steps (when Webull is available)
- Replace Yahoo with Webull market data.
- Implement execution adapter and order state machine.
- Add reconciliation and true fill-based PnL.
