"""Abstract execution interface and shared dataclasses for futures trading.

Distinct from the equities/options ExecutionAdapter in `src/execution_adapter.py`
because the order payload, fill semantics, and reconciliation surface differ
enough that a single ABC trying to cover both ends up confusingly type-loose.

The bracket primitives (`BracketIntent`, `BracketAck`, `PositionStatus`,
`submit_bracket`/`poll_position`/`flatten_position`) are central to live
deployment: they let the broker hold the OCO TP+stop server-side, so a
client-side blip can't strand a position. The runner stops computing exits
from bars and instead polls the adapter for status changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class FuturesOrderIntent:
    """A futures order request, before submission."""

    symbol: str               # "ES", "NQ", "MNQ", etc.
    side: str                 # "BUY" | "SELL"
    qty: int                  # contracts
    order_type: str = "MARKET"   # "MARKET" | "LIMIT" | "STOP" | "STOP_LIMIT"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tif: str = "DAY"
    client_order_id: Optional[str] = None
    intent: str = "entry"     # "entry" | "tp" | "stop" | "time_stop"


@dataclass(frozen=True)
class FuturesOrderAck:
    """Adapter-returned acknowledgment of an order submission."""

    status: str               # "filled" | "submitted" | "rejected" | "cancelled" | "pending"
    order_id: Optional[str] = None
    filled_qty: Optional[int] = None
    fill_price: Optional[float] = None
    reason: Optional[str] = None
    submission_time_utc: Optional[str] = None
    fill_time_utc: Optional[str] = None
    realized_pnl: Optional[float] = None


@dataclass(frozen=True)
class FuturesQuote:
    """A snapshot of NBBO for a futures symbol."""

    symbol: str
    bid: float
    ask: float
    quote_time_utc: str


# --- Bracket primitives -------------------------------------------------


@dataclass(frozen=True)
class BracketIntent:
    """An entry order plus the TP and SL legs that must accompany it.

    `take_profit_price` and `stop_loss_price` are absolute prices, computed
    by the caller from the latest bar close (since MARKET parents fill at
    an unknowable price, the bracket children must be priced from a
    reference rather than offsets-from-fill).
    """

    entry: "FuturesOrderIntent"
    take_profit_price: float
    stop_loss_price: float
    bracket_id: Optional[str] = None


@dataclass(frozen=True)
class BracketAck:
    """Adapter-returned acknowledgment of a bracket submission."""

    status: str                          # "active" | "rejected" | "partial"
    entry_ack: "FuturesOrderAck"
    take_profit_order_id: Optional[str] = None
    stop_loss_order_id: Optional[str] = None
    bracket_id: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class PositionStatus:
    """Adapter's view of a symbol's current position state.

    `state` is the runner's main signal. Transitions:
      "flat"          → no position open, no recent close
      "open"          → position currently open at the broker
      "closed_tp"     → TP child filled since last poll
      "closed_stop"   → SL child filled since last poll
      "closed_other"  → position closed for some other reason (manual flatten,
                        broker-side action, session_close offset, etc.)
      "unknown"       → adapter could not determine state (transient broker
                        query failure, disconnect, etc.). Runner MUST treat
                        unknown as "preserve local state, retry next tick" —
                        a transient failure must not cause loss of position
                        tracking. Distinct from "flat", which is a confident
                        broker-confirmed verdict.

    Adapters MUST clear closed-state after reporting it once so subsequent
    polls return "flat". This is an edge-triggered signal, not level.
    """

    state: str
    side: Optional[str] = None              # "BUY" | "SELL" when state == "open"
    qty: int = 0
    entry_price: Optional[float] = None
    last_fill_price: Optional[float] = None
    realized_pnl: Optional[float] = None
    note: str = ""


class FuturesQuoteProvider(Protocol):
    """Quote source the paper adapter uses to compute simulated fills.

    Production implementations might wrap a Webull/Tradovate/IBKR live feed.
    Tests inject deterministic fakes.
    """

    def get_quote(self, symbol: str) -> FuturesQuote: ...


class FuturesExecutionAdapter(ABC):
    """Common contract for live or paper futures execution."""

    @abstractmethod
    def submit_order(self, intent: FuturesOrderIntent) -> FuturesOrderAck:
        """Submit a single order. Used for naked entries/exits without OCO.

        Production code should prefer `submit_bracket` for entries so the
        broker holds the TP/SL server-side. `submit_order` remains for
        manual flattens, smoke tests, and adapter-internal flows.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> FuturesOrderAck:
        """Cancel a working order."""

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        """Return current open positions as dicts (symbol, side, qty, entry_price, ...)."""

    @abstractmethod
    def reconcile(self) -> dict:
        """Produce a reconciliation summary (positions count, working order count, etc)."""

    @abstractmethod
    def submit_bracket(self, intent: BracketIntent) -> BracketAck:
        """Submit an entry order with paired TP and SL legs as an OCO bracket.

        Live adapters wire the bracket server-side at the broker so OCO
        survives client-side disconnects. Paper adapters track virtual
        TP/SL prices and resolve them on `poll_position`.
        """

    @abstractmethod
    def poll_position(
        self,
        symbol: str,
        *,
        reference_price: Optional[float] = None,
    ) -> PositionStatus:
        """Return current `PositionStatus` for `symbol`.

        Closed states (closed_tp / closed_stop / closed_other) are
        edge-triggered: the adapter MUST clear them after reporting once,
        so subsequent calls return "flat" until a new bracket is submitted.

        `reference_price` is used by paper adapters to evaluate virtual
        TP/SL against the latest bar close. Live adapters typically ignore
        it because broker-side OCO already fired or didn't.
        """

    @abstractmethod
    def flatten_position(
        self,
        symbol: str,
        *,
        reason: str = "",
    ) -> FuturesOrderAck:
        """Force-close any open position for `symbol`.

        Cancels active bracket children (so the offsetting MARKET doesn't
        race with the broker's OCO), then submits an offsetting MARKET.
        Used for session-close exits and manual interventions.
        """
