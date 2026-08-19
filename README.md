# Trading Bot

An intraday options research system. It is a decision pipeline, not an execution system:
it reads market data, evaluates a setup, selects a contract, applies risk controls, and
emits an ALLOWED or REJECTED plan with the reasons attached.

Paper and research only. No live trading, and no claim of profitability.

## Pipeline

```
market data -> signal engine -> instrument selection -> risk engine -> plan -> journal
```

Each stage is an independent gate behind an interface in `src/interfaces.py`, so providers
and strategies swap without touching the rest.

- **Data** (`src/data/`) — intraday bars and option chains, with a primary provider and
  fallbacks. Staleness gating rejects bars and quotes that are too old to act on.
- **Signals** (`src/engines/`) — VWAP pullback, opening-range breakout, and mean reversion,
  with regime and time-of-day filters.
- **Instruments** (`src/services/`) — scores nearby strikes on liquidity, greeks, and spread
  penalties, then picks the best candidate that clears the gates.
- **Risk** (`src/risk.py`) — position sizing plus hard limits: kill switch, daily loss
  lockout, cooldowns, duplicate-order windows, per-symbol and total contract caps,
  notional caps. Any failing gate blocks the trade and records why.
- **Journal** (`src/journal.py`) — append-only event log with UTC timestamps and
  correlation IDs, so any decision can be replayed (`src/replay.py`).

## Backtesting

`src/backtest/` and `src/futures_backtest/` run walk-forward evaluation with out-of-sample
holdouts. `scripts/` holds the sweep, holdout, and comparison runners, plus historical data
downloaders.

Backtests model slippage (`src/slippage.py`, `src/futures_slippage.py`) and compare
close-only against intrabar fills, since close-only results are optimistic relative to
what live execution would produce.

## Setup

```sh
pip install -r requirements.txt
cp .env.example .env      # fill in the provider keys you have
py -m src                 # run the planner
py -m src --web           # planner + local dashboard
pytest                    # tests
```

Credentials live in `.env` only. The loader refuses to start if it finds plaintext
credentials in `config.json`. Strategy rules, risk limits, and output settings are in
`config.json`.

## Layout

```
src/           pipeline, engines, risk, journal, web dashboard
src/backtest/  options walk-forward backtester
src/futures_*  futures backtest and execution modelling
scripts/       sweeps, holdouts, data downloads, scheduler
tests/         unit and integration tests
web/           dashboard assets
```
