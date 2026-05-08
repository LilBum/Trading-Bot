# Trading SOP Checklist

This checklist supports consistent, repeatable operation of the planner. It does not authorize trading.

## Pre-market
- Confirm data provider health and network stability.
- Review economic calendar for high-impact events.
- Verify `config.json` limits: risk %, max trades, daily loss lockout.
- Confirm sentiment/regime inputs (if enabled) are updated.
- Start the planner in watch or web mode and verify fresh data timestamps.

## Trade review (per signal)
- Confirm setup matches the intended rule set (VWAP pullback + regime filters).
- Confirm higher-timeframe alignment (if enabled).
- Confirm option liquidity gates pass (spread, OI, volume, quote age).
- Confirm position size and risk fit the plan limits.
- Confirm stop/targets match the plan (premium % or ATR-based).

## Post-market
- Record realized PnL updates (`py -m src --record-pnl`).
- Review reject reasons and data health scores.
- Note any provider errors or stale data warnings.
- Update the journal with any manual execution notes.
