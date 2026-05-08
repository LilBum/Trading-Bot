"""Position records for the backtest runner.

OpenPosition is the live state of a held option contract; ClosedTrade is
the immutable record produced when an OpenPosition exits, ready for
metrics aggregation. PnL is computed in dollar terms (per-contract price
delta * contract_multiplier * contracts), with the buy-side sign baked in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OpenPosition:
    """A long option position held during a backtest session."""

    symbol: str
    direction: str               # "CALL" | "PUT"
    strike: float
    expiration_date: str         # YYYY-MM-DD ET
    contracts: int
    entry_time_et: datetime
    entry_price: float           # per-contract premium paid (post-slippage)
    entry_underlying: float
    entry_iv: float
    entry_delta: float
    entry_gamma: float
    entry_theta_per_day: float
    take_profit_pct: float       # 0.30 = +30% on premium
    stop_loss_pct: float         # 0.25 = -25% on premium
    max_hold_minutes: int        # absolute hold time stop
    contract_multiplier: int = 100


@dataclass(frozen=True)
class ClosedTrade:
    """A completed round-trip trade record for metrics."""

    symbol: str
    direction: str
    strike: float
    contracts: int
    entry_time_et: datetime
    exit_time_et: datetime
    entry_price: float
    exit_price: float
    realized_pnl: float
    exit_reason: str             # "tp" | "stop" | "time_stop" | "session_close"
    holding_minutes: float
    contract_multiplier: int = 100


def realized_pnl(
    entry_price: float,
    exit_price: float,
    contracts: int,
    contract_multiplier: int = 100,
) -> float:
    """Long-only PnL: (exit - entry) * contracts * multiplier."""
    return (exit_price - entry_price) * contracts * contract_multiplier


def close_position(
    position: OpenPosition,
    *,
    exit_time_et: datetime,
    exit_price: float,
    exit_reason: str,
) -> ClosedTrade:
    holding_seconds = (exit_time_et - position.entry_time_et).total_seconds()
    return ClosedTrade(
        symbol=position.symbol,
        direction=position.direction,
        strike=position.strike,
        contracts=position.contracts,
        entry_time_et=position.entry_time_et,
        exit_time_et=exit_time_et,
        entry_price=position.entry_price,
        exit_price=exit_price,
        realized_pnl=realized_pnl(
            position.entry_price, exit_price, position.contracts, position.contract_multiplier
        ),
        exit_reason=exit_reason,
        holding_minutes=holding_seconds / 60.0,
        contract_multiplier=position.contract_multiplier,
    )


def evaluate_exit(
    position: OpenPosition,
    *,
    current_option_price: float,
    current_time_et: datetime,
    minutes_to_session_close: float,
    exit_before_close_minutes: float,
) -> str | None:
    """Return an exit reason if any trigger hits, else None.

    Order of precedence: TP > SL > session-close-buffer > time-stop.
    """
    pct_change = (current_option_price - position.entry_price) / position.entry_price
    if pct_change >= position.take_profit_pct:
        return "tp"
    if pct_change <= -position.stop_loss_pct:
        return "stop"
    if minutes_to_session_close <= exit_before_close_minutes:
        return "session_close"
    holding_minutes = (current_time_et - position.entry_time_et).total_seconds() / 60.0
    if holding_minutes >= position.max_hold_minutes:
        return "time_stop"
    return None
