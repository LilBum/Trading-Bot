"""Position records for the futures backtest runner.

Differences from src/backtest/positions.py (which models options):
- `side` is BUY or SELL (futures can be short, options bot was long-only).
- No strike/expiration/iv/delta/gamma/theta.
- Stops and targets are in price points, not percent of premium.
- PnL = direction × (exit - entry) × point_value × contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FuturesOpenPosition:
    """A long or short futures position held during a backtest session."""

    symbol: str               # "ES", "NQ", etc.
    side: str                 # "BUY" (long) | "SELL" (short)
    contracts: int
    entry_time_et: datetime
    entry_price: float        # in price points
    point_value: float        # dollars per point (50 for ES, 20 for NQ)
    tick_size: float          # 0.25 for ES/NQ
    take_profit_points: float # absolute points of favorable move
    stop_loss_points: float   # absolute points of adverse move
    max_hold_minutes: int


@dataclass(frozen=True)
class FuturesClosedTrade:
    """Completed round-trip futures trade record."""

    symbol: str
    side: str
    contracts: int
    entry_time_et: datetime
    exit_time_et: datetime
    entry_price: float
    exit_price: float
    realized_points: float
    realized_pnl: float       # dollars
    point_value: float
    exit_reason: str          # "tp" | "stop" | "session_close" | "time_stop"
    holding_minutes: float


def realized_pnl_points(
    side: str,
    entry_price: float,
    exit_price: float,
    contracts: int,
) -> float:
    """Points × contracts. Positive for profitable trades regardless of side."""
    direction = +1 if side.upper() == "BUY" else -1
    return direction * (exit_price - entry_price) * contracts


def realized_pnl_dollars(
    side: str,
    entry_price: float,
    exit_price: float,
    contracts: int,
    point_value: float,
) -> float:
    return realized_pnl_points(side, entry_price, exit_price, contracts) * point_value


def close_position(
    position: FuturesOpenPosition,
    *,
    exit_time_et: datetime,
    exit_price: float,
    exit_reason: str,
) -> FuturesClosedTrade:
    holding_seconds = (exit_time_et - position.entry_time_et).total_seconds()
    points = realized_pnl_points(position.side, position.entry_price, exit_price, position.contracts)
    return FuturesClosedTrade(
        symbol=position.symbol,
        side=position.side,
        contracts=position.contracts,
        entry_time_et=position.entry_time_et,
        exit_time_et=exit_time_et,
        entry_price=position.entry_price,
        exit_price=exit_price,
        realized_points=points,
        realized_pnl=points * position.point_value,
        point_value=position.point_value,
        exit_reason=exit_reason,
        holding_minutes=holding_seconds / 60.0,
    )


def evaluate_exit(
    position: FuturesOpenPosition,
    *,
    current_price: float,
    current_time_et: datetime,
    minutes_to_session_close: float,
    exit_before_close_minutes: float,
) -> str | None:
    """Return an exit reason if any trigger hits, else None.

    Close-only evaluation: TP/SL fire only when the bar Close has crossed
    the threshold. Misses intrabar TP/SL fires that pulled back before
    the close. Use `evaluate_exit_intrabar` for production-aligned
    behaviour (broker-side OCO triggers on tick prints, not bar closes).

    Precedence: TP > stop > session_close > time_stop. Stops and targets
    measured against the directional move from entry, in price points.
    """
    direction = +1 if position.side.upper() == "BUY" else -1
    move_points = direction * (current_price - position.entry_price)
    if move_points >= position.take_profit_points:
        return "tp"
    if move_points <= -position.stop_loss_points:
        return "stop"
    if minutes_to_session_close <= exit_before_close_minutes:
        return "session_close"
    holding_minutes = (current_time_et - position.entry_time_et).total_seconds() / 60.0
    if holding_minutes >= position.max_hold_minutes:
        return "time_stop"
    return None


def evaluate_exit_intrabar(
    position: FuturesOpenPosition,
    *,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    current_time_et: datetime,
    minutes_to_session_close: float,
    exit_before_close_minutes: float,
    prefer_stop_when_both_hit: bool = True,
) -> tuple[str | None, float | None]:
    """Evaluate exits using intrabar HIGH/LOW for TP/SL detection.

    Returns `(exit_reason, exit_price)` or `(None, None)` if no exit fires.
    `exit_price` is the broker-side bracket trigger price for TP/SL legs
    and the bar's Close for session_close / time_stop. The runner layers
    slippage on top of this to produce the realized fill.

    Why intrabar matters: a 1-minute bar with high=entry+150, low=entry-50,
    close=entry+50 has BOTH crossed a 100pt TP and a 50pt stop, but with
    close-only evaluation the runner sees Close=+50 and holds — missing
    the TP fire entirely. In live trading the broker's OCO would have
    fired on the first tick that crossed the trigger.

    Precedence:
      1. Both TP and stop hit intrabar → prefer stop (conservative; we
         can't know which crossed first from a single bar). Configurable
         via `prefer_stop_when_both_hit`.
      2. Only TP intrabar → tp at tp_price.
      3. Only stop intrabar → stop at sl_price.
      4. Else session_close / time_stop fired at bar_close.
    """
    direction = +1 if position.side.upper() == "BUY" else -1

    if direction > 0:
        tp_price = position.entry_price + position.take_profit_points
        sl_price = position.entry_price - position.stop_loss_points
        tp_hit = bar_high >= tp_price
        sl_hit = bar_low <= sl_price
    else:
        tp_price = position.entry_price - position.take_profit_points
        sl_price = position.entry_price + position.stop_loss_points
        tp_hit = bar_low <= tp_price
        sl_hit = bar_high >= sl_price

    if tp_hit and sl_hit:
        if prefer_stop_when_both_hit:
            return ("stop", sl_price)
        return ("tp", tp_price)
    if tp_hit:
        return ("tp", tp_price)
    if sl_hit:
        return ("stop", sl_price)

    if minutes_to_session_close <= exit_before_close_minutes:
        return ("session_close", bar_close)

    holding_minutes = (current_time_et - position.entry_time_et).total_seconds() / 60.0
    if holding_minutes >= position.max_hold_minutes:
        return ("time_stop", bar_close)

    return (None, None)
