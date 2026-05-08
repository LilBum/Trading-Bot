# Changelog

All notable changes to this project are documented here.

## Unreleased
- Added a web dashboard with auto-refresh and a local JSON API.
- Added watch mode and JSON-configurable intervals.
- Added interface-driven architecture: signal engine, instrument service, risk engine, provider, and planner.
- Added option chain scoring across nearby strikes with liquidity gates, greeks, and penalties.
- Added data staleness gating for bars and quotes.
- Added structured event logging with UTC timestamps, run/decision IDs, and provider metadata.
- Added risk controls: kill switch, data-health gating, throttles, duplicate detection, max per-symbol and total contracts, notional caps.
- Added daily loss lockout and cooldown checks based on event log state.
- Added provider selection with Yahoo default and Webull stub + Yahoo fallback.
- Added tests for risk controls and option selection behavior.
- Simplified CLI output modes and ensured strike/expiration visibility.
- Added higher-timeframe EMA alignment and ATR calculations in the signal engine.
- Added optional sentiment regime filtering (manual or CNN Fear & Greed).
- Added volatility-adjusted position sizing and ATR-based stop mode support.
- Logged volatility/sentiment context in order-intent events.
- Switched sentiment gating to advisory by default with configurable hard-block mode.
- Made IV deviation checks skew-aware using a moneyness band.
- Logged base vs adjusted risk sizing context for audit.
- Added config comment support and tests for config/sentiment/IV band behavior.
- Added dashboard session totals sourced from the event log.
- Dashboard now persists accepted plans and shows underlying price.
- Added hybrid provider (Webull bars + Yahoo options chain) and Webull SDK integration.
- Added Public API data provider for quotes/options with Yahoo bars fallback.
- Added Finnhub intraday bars support for the Public provider.
