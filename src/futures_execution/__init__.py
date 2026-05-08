"""Live execution layer for futures trading.

Two adapter implementations:
- PaperFuturesExecutionAdapter — pure internal simulation using the same
  slippage model the backtest harness uses. Paper PnL should track backtest
  PnL, which is the integrity check for "the strategy actually does what we
  measured."
- IBKRFuturesExecutionAdapter — live/paper via IB Gateway or TWS using
  ib_insync. Uses front-month real Future contracts (not ContFuture, which
  IBKR documents as historical-data-only).
"""

from src.futures_execution.adapter import (
    BracketAck,
    BracketIntent,
    FuturesExecutionAdapter,
    FuturesOrderAck,
    FuturesOrderIntent,
    FuturesQuote,
    FuturesQuoteProvider,
    PositionStatus,
)
from src.futures_execution.paper import (
    PaperFuturesExecutionAdapter,
    PaperPositionRecord,
)

__all__ = [
    "BracketAck",
    "BracketIntent",
    "FuturesExecutionAdapter",
    "FuturesOrderAck",
    "FuturesOrderIntent",
    "FuturesQuote",
    "FuturesQuoteProvider",
    "PaperFuturesExecutionAdapter",
    "PaperPositionRecord",
    "PositionStatus",
]
