"""Real-time orchestrator: signal -> bracket -> adapter -> position tracking.

One `run_iteration()` represents one cycle (typically once per minute):
  1. Fetch the latest session bars via the injected BarsProvider.
  2. If a position is open, poll the adapter for status. If a bracket leg
     fired (closed_tp / closed_stop), clear state. Otherwise check the
     session-close buffer and flatten if we're inside it.
  3. If no position is open and we haven't entered today, run the signal
     engine on the bars view; if it returns CALL/PUT, build a bracket
     intent (entry MARKET + TP LIMIT + SL STOP) and submit it as one
     atomic OCO via `adapter.submit_bracket`.

The runner does NOT compute TP/SL exits from bars itself. The broker holds
the OCO server-side; we just poll for status. This is the safety primitive
that lets us survive a network blip or process restart without stranding
a naked position.

State (open position, entered_today, last bar time) lives in `LiveRunnerState`
and persists across iterations within the same process. Cross-process
recovery is handled by `recover_state_from_broker()`, called once at start.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from src.futures_execution.adapter import (
    BracketIntent,
    FuturesExecutionAdapter,
    FuturesOrderAck,
    FuturesOrderIntent,
    PositionStatus,
)


EASTERN = ZoneInfo("America/New_York")


class BarsProvider(Protocol):
    """Source of OHLCV bars for the current trading session.

    Production: IBKR-backed implementation pulling reqHistoricalData.
    Tests: in-memory fakes that return whatever bars the test injected.
    """

    def get_session_bars(self, symbol: str) -> pd.DataFrame: ...


@dataclass(frozen=True)
class LivePaperRunnerConfig:
    take_profit_points: float = 100.0
    stop_loss_points: float = 50.0
    contracts_per_trade: int = 1
    session_start_et: time = time(8, 0)
    session_end_et: time = time(16, 0)
    exit_before_close_minutes: int = 5
    min_signal_bars: int = 16


@dataclass
class LiveRunnerState:
    open_position_side: Optional[str] = None      # "BUY" or "SELL"
    open_position_qty: int = 0
    open_position_entry_price: Optional[float] = None
    open_position_entry_time_utc: Optional[str] = None
    open_position_order_id: Optional[str] = None
    entered_today: bool = False
    # Bracket bookkeeping. Set when submit_bracket succeeds; cleared when
    # the position closes (via TP/SL/flatten).
    active_bracket_id: Optional[str] = None
    active_tp_order_id: Optional[str] = None
    active_sl_order_id: Optional[str] = None


@dataclass(frozen=True)
class IterationResult:
    """Outcome of one run_iteration call. Used for logging and tests."""

    timestamp_et: datetime
    bars_count: int
    signal_direction: Optional[str]      # "CALL" | "PUT" | "NONE" | None
    action: str                           # see _Actions below
    order_ack: Optional[FuturesOrderAck] = None
    note: str = ""


class _Actions:
    WARMUP = "warmup"                # not enough bars yet
    NO_SIGNAL = "no_signal"          # signal engine returned NONE
    SIGNAL_ERROR = "signal_error"    # exception inside signal engine
    ENTRY_FILLED = "entry_filled"
    ENTRY_SUBMITTED = "entry_submitted"  # bracket at broker, parent fill pending
    ENTRY_REJECTED = "entry_rejected"
    PENDING_ENTRY_HOLD = "pending_entry_hold"  # parent still hasn't filled
    EXIT_TP = "exit_tp"
    EXIT_STOP = "exit_stop"
    EXIT_SESSION_CLOSE = "exit_session_close"
    EXIT_REJECTED = "exit_rejected"
    HOLD = "hold"                    # position open and no exit triggered


class LivePaperRunner:
    """One signal/execution pair, one trading day.

    `signal_symbol` and `execution_symbol` are usually equal, but can differ
    for the NQ→MNQ routing pattern: signal generated from full-size NQ bars
    (where the edge was measured), orders sent to MNQ (1/10th risk for
    shakedown). Pass `symbol=...` for the equal-case back-compat shortcut.

    Caller drives the cadence (one iteration per minute typical, more often
    for tighter monitoring).
    """

    def __init__(
        self,
        symbol: Optional[str] = None,
        signal_engine=None,
        execution_adapter: Optional[FuturesExecutionAdapter] = None,
        bars_provider: Optional[BarsProvider] = None,
        config: Optional[LivePaperRunnerConfig] = None,
        state: Optional[LiveRunnerState] = None,
        *,
        signal_symbol: Optional[str] = None,
        execution_symbol: Optional[str] = None,
    ) -> None:
        if signal_symbol is None and execution_symbol is None:
            if symbol is None:
                raise ValueError(
                    "must provide either `symbol=` or both `signal_symbol=` "
                    "and `execution_symbol=`"
                )
            signal_symbol = symbol
            execution_symbol = symbol
        elif signal_symbol is None or execution_symbol is None:
            raise ValueError(
                "provide both signal_symbol and execution_symbol, "
                "or just `symbol` for the equal case"
            )
        if signal_engine is None:
            raise ValueError("signal_engine is required")
        if execution_adapter is None:
            raise ValueError("execution_adapter is required")
        if bars_provider is None:
            raise ValueError("bars_provider is required")
        if config is None:
            raise ValueError("config is required")

        self.signal_symbol = signal_symbol
        self.execution_symbol = execution_symbol
        # `self.symbol` is preserved as an alias for the execution side
        # (the side that owns position/PnL accounting). Code reading
        # `self.symbol` was previously trading and tracking the same instrument
        # so this preserves behaviour for the equal case.
        self.symbol = execution_symbol
        self.signal_engine = signal_engine
        self.execution_adapter = execution_adapter
        self.bars_provider = bars_provider
        self.config = config
        self.state = state or LiveRunnerState()

    def run_iteration(self, current_time_et: Optional[datetime] = None) -> IterationResult:
        now_et = current_time_et or datetime.now(EASTERN)
        if now_et.tzinfo is None:
            now_et = now_et.replace(tzinfo=EASTERN)

        # Bars and signals come from the SIGNAL symbol (where the edge was
        # measured). Orders route to the EXECUTION symbol below.
        bars = self.bars_provider.get_session_bars(self.signal_symbol)
        bars_count = len(bars)
        if bars_count < self.config.min_signal_bars:
            return IterationResult(
                timestamp_et=now_et,
                bars_count=bars_count,
                signal_direction=None,
                action=_Actions.WARMUP,
                note=f"need {self.config.min_signal_bars} bars, have {bars_count}",
            )

        if self.state.open_position_side is not None:
            exit_result = self._maybe_exit(bars, now_et)
            if exit_result is not None:
                return exit_result

        # Pending-entry path: a bracket was submitted but the parent hasn't
        # filled yet. Poll the adapter to see if the fill ack landed, or
        # whether the parent was rejected/cancelled at the broker.
        if (
            self.state.open_position_side is None
            and self.state.active_bracket_id is not None
        ):
            return self._maybe_resolve_pending_entry(bars, now_et)

        if self.state.open_position_side is None and not self.state.entered_today:
            return self._maybe_enter(bars, now_et)

        return IterationResult(
            timestamp_et=now_et,
            bars_count=bars_count,
            signal_direction=None,
            action=_Actions.HOLD,
            note="position open or already entered today",
        )

    # ---- entry path ---------------------------------------------------

    def _maybe_enter(self, bars: pd.DataFrame, now_et: datetime) -> IterationResult:
        runtime_config = {
            "data_quality": {"max_bar_age_minutes": None, "allow_stale_when_closed": True},
            "position_sizing": {"stop_mode": "points"},
            "sentiment": {"enabled": False},
        }
        try:
            signal = self.signal_engine.evaluate(
                self.signal_symbol, bars, runtime_config
            )
        except Exception as exc:
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.SIGNAL_ERROR,
                note=str(exc),
            )

        if signal.direction not in ("CALL", "PUT") or signal.reject_reasons:
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=signal.direction,
                action=_Actions.NO_SIGNAL,
                note=", ".join(signal.reject_reasons) if signal.reject_reasons else "no setup",
            )

        side = "BUY" if signal.direction == "CALL" else "SELL"

        # Compute TP/SL prices from the latest bar close. Since the parent is
        # MARKET we can't anchor them to actual fill — but for NQ at 100/50pt
        # exits, a 1-3pt displacement from expected fill is well below the
        # noise floor of the strategy.
        ref_price = float(bars["Close"].iloc[-1])
        if side == "BUY":
            tp_price = ref_price + self.config.take_profit_points
            sl_price = ref_price - self.config.stop_loss_points
        else:
            tp_price = ref_price - self.config.take_profit_points
            sl_price = ref_price + self.config.stop_loss_points

        bracket_id = f"orb-{self.execution_symbol}-{uuid.uuid4().hex[:8]}"
        bracket_intent = BracketIntent(
            entry=FuturesOrderIntent(
                symbol=self.execution_symbol,
                side=side,
                qty=self.config.contracts_per_trade,
                order_type="MARKET",
                intent="entry",
                client_order_id=bracket_id,
            ),
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
            bracket_id=bracket_id,
        )

        bracket_ack = self.execution_adapter.submit_bracket(bracket_intent)
        entry_ack = bracket_ack.entry_ack

        if bracket_ack.status == "active":
            # Bracket is at the broker. Track its IDs and lock entered_today
            # regardless of whether the parent has filled yet — we don't want
            # a re-entry while we're waiting on the fill ack.
            self.state.active_bracket_id = bracket_ack.bracket_id
            self.state.active_tp_order_id = bracket_ack.take_profit_order_id
            self.state.active_sl_order_id = bracket_ack.stop_loss_order_id
            self.state.entered_today = True

            if entry_ack.status == "filled":
                # Synchronous fill ack — populate full position state.
                self.state.open_position_side = side
                self.state.open_position_qty = int(
                    entry_ack.filled_qty or self.config.contracts_per_trade
                )
                self.state.open_position_entry_price = (
                    float(entry_ack.fill_price)
                    if entry_ack.fill_price is not None else ref_price
                )
                self.state.open_position_entry_time_utc = (
                    entry_ack.fill_time_utc or entry_ack.submission_time_utc
                )
                self.state.open_position_order_id = entry_ack.order_id
                return IterationResult(
                    timestamp_et=now_et,
                    bars_count=len(bars),
                    signal_direction=signal.direction,
                    action=_Actions.ENTRY_FILLED,
                    order_ack=entry_ack,
                    note=(
                        f"{side} {self.config.contracts_per_trade} {self.execution_symbol} @ "
                        f"{entry_ack.fill_price}; bracket TP={tp_price} SL={sl_price}"
                    ),
                )

            if entry_ack.status == "submitted":
                # Parent at broker but fill ack hasn't arrived. Note: we do
                # NOT populate open_position_side because we don't have a
                # confirmed fill yet. Next iteration's resolve-pending-entry
                # path will poll the broker and transition to ENTRY_FILLED
                # once the fill lands, or clear state if it was cancelled.
                return IterationResult(
                    timestamp_et=now_et,
                    bars_count=len(bars),
                    signal_direction=signal.direction,
                    action=_Actions.ENTRY_SUBMITTED,
                    order_ack=entry_ack,
                    note=(
                        f"bracket {bracket_ack.bracket_id} submitted; parent fill pending. "
                        f"Children: TP={tp_price} SL={sl_price}"
                    ),
                )

            # Bracket reported active but entry status is something we don't
            # know how to interpret (e.g. cancelled by broker between submit
            # and ack). Treat as rejection and clear bracket state so we
            # don't think we're managing a working bracket.
            self.state.active_bracket_id = None
            self.state.active_tp_order_id = None
            self.state.active_sl_order_id = None

        return IterationResult(
            timestamp_et=now_et,
            bars_count=len(bars),
            signal_direction=signal.direction,
            action=_Actions.ENTRY_REJECTED,
            order_ack=entry_ack,
            note=bracket_ack.reason or entry_ack.reason or "bracket not active",
        )

    def _maybe_resolve_pending_entry(
        self, bars: pd.DataFrame, now_et: datetime
    ) -> IterationResult:
        """Poll the broker for a fill on a previously-submitted bracket parent.

        Reachable when `active_bracket_id` is set but `open_position_side` is
        not — i.e. the parent went out as Submitted, not Filled. Possible
        outcomes per poll:

          - Broker now reports an open position: parent filled, transition
            to ENTRY_FILLED with full position state.
          - Broker reports flat AND closed_tp/closed_stop: bracket already
            resolved (extreme edge case where TP/SL fired before we saw
            the parent fill). Clear state and report the appropriate exit.
          - Broker reports flat with bracket_id no longer in adapter: parent
            was rejected or cancelled. Clear state and report ENTRY_REJECTED.
          - Broker reports flat with bracket still tracked: still pending,
            keep waiting.
        """
        current_close = float(bars["Close"].iloc[-1]) if len(bars) else 0.0
        try:
            status = self.execution_adapter.poll_position(
                self.execution_symbol,
                reference_price=current_close,
            )
        except Exception as exc:
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.PENDING_ENTRY_HOLD,
                note=f"poll_position failed during pending-entry resolve: {exc}",
            )

        if status.state == "open":
            # Parent filled. Promote pending state to actual open position.
            self.state.open_position_side = status.side
            self.state.open_position_qty = int(status.qty)
            self.state.open_position_entry_price = status.entry_price
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.ENTRY_FILLED,
                note=(
                    f"pending bracket resolved: {status.side} "
                    f"{status.qty} {self.execution_symbol} @ {status.entry_price}"
                ),
            )

        if status.state in ("closed_tp", "closed_stop", "closed_other"):
            # Rare: bracket fired between submit and our first poll.
            self._clear_position()
            action = (
                _Actions.EXIT_TP if status.state == "closed_tp" else
                _Actions.EXIT_STOP if status.state == "closed_stop" else
                _Actions.EXIT_SESSION_CLOSE
            )
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=action,
                note=f"bracket resolved during pending-entry: {status.note}",
            )

        if status.state == "unknown":
            # Broker query failed. Keep pending state intact and retry.
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.PENDING_ENTRY_HOLD,
                note=f"adapter unknown during pending-entry: {status.note}",
            )

        # status.state == "flat" with bracket still pending — keep waiting.
        return IterationResult(
            timestamp_et=now_et,
            bars_count=len(bars),
            signal_direction=None,
            action=_Actions.PENDING_ENTRY_HOLD,
            note=(
                f"bracket {self.state.active_bracket_id} still pending parent fill"
            ),
        )

    # ---- exit path ----------------------------------------------------

    def _maybe_exit(self, bars: pd.DataFrame, now_et: datetime) -> Optional[IterationResult]:
        if not len(bars):
            return None

        current_close = float(bars["Close"].iloc[-1])
        # Poll the adapter — broker (or paper sim) is the source of truth
        # for whether a bracket leg fired.
        try:
            status = self.execution_adapter.poll_position(
                self.execution_symbol,
                reference_price=current_close,
            )
        except Exception as exc:
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.EXIT_REJECTED,
                note=f"poll_position failed: {exc}",
            )

        if status.state == "closed_tp":
            self._clear_position()
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.EXIT_TP,
                note=status.note or f"TP filled @ {status.last_fill_price}",
            )
        if status.state == "closed_stop":
            self._clear_position()
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.EXIT_STOP,
                note=status.note or f"SL filled @ {status.last_fill_price}",
            )
        if status.state == "closed_other":
            # Rare: position closed for a reason we didn't initiate (manual
            # broker action, kill switch, etc.). Still need to clear local state.
            self._clear_position()
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.EXIT_SESSION_CLOSE,
                note=status.note or "position closed externally",
            )
        if status.state == "unknown":
            # Adapter couldn't determine state (broker query failed). DO NOT
            # clear local state — a transient query failure must not cause
            # us to drop tracking of a real open position. Hold and retry.
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.HOLD,
                note=f"adapter unknown; preserving state: {status.note}",
            )
        if status.state == "flat":
            # Adapter says we're flat but runner thinks we're open. This is
            # a confident broker-side verdict (positions query succeeded and
            # returned no position), distinct from "unknown" above. Trust
            # the adapter — the position was likely closed externally
            # (manual flatten in TWS, kill switch, broker-side action).
            self._clear_position()
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.EXIT_SESSION_CLOSE,
                note="adapter reports flat; clearing runner state",
            )

        # status.state == "open": bracket still working at the broker.
        # Check session-close buffer; if we're inside it, force-flatten.
        session_close_dt = now_et.replace(
            hour=self.config.session_end_et.hour,
            minute=self.config.session_end_et.minute,
            second=0,
            microsecond=0,
        )
        minutes_to_close = (session_close_dt - now_et).total_seconds() / 60.0
        if minutes_to_close <= self.config.exit_before_close_minutes:
            ack = self.execution_adapter.flatten_position(
                self.execution_symbol, reason="session_close"
            )
            if ack.status == "filled":
                # Confirmed offset filled; safe to clear local state.
                self._clear_position()
                return IterationResult(
                    timestamp_et=now_et,
                    bars_count=len(bars),
                    signal_direction=None,
                    action=_Actions.EXIT_SESSION_CLOSE,
                    order_ack=ack,
                    note=f"session-close flatten @ {ack.fill_price}",
                )
            # Flatten failed — DO NOT clear local state. The position is
            # still open at the broker. Next iteration will see the open
            # position again and retry the flatten. Better to be loud
            # about a stuck-open position than silently lose tracking.
            return IterationResult(
                timestamp_et=now_et,
                bars_count=len(bars),
                signal_direction=None,
                action=_Actions.EXIT_REJECTED,
                order_ack=ack,
                note=(
                    f"session-close flatten failed; will retry next tick. "
                    f"reason: {ack.reason or 'unknown'}"
                ),
            )

        return IterationResult(
            timestamp_et=now_et,
            bars_count=len(bars),
            signal_direction=None,
            action=_Actions.HOLD,
            note=(
                f"{status.side} open qty={status.qty} @ {status.entry_price}; "
                f"current {current_close}; bracket active"
            ),
        )

    def recover_state_from_broker(self) -> Optional[PositionStatus]:
        """Reconcile-on-start: query the adapter for any pre-existing position.

        Call this once after constructing the runner and before the first
        `run_iteration()`. If the broker reports an open position for our
        symbol (e.g. left over from a previous process run that crashed
        mid-trade), we recover state from it instead of entering fresh
        and ending up with a doubled position.

        Returns the PositionStatus that was recovered (or None if flat).
        """
        try:
            status = self.execution_adapter.poll_position(self.execution_symbol)
        except Exception:
            return None

        if status.state != "open":
            return None

        self.state.open_position_side = status.side
        self.state.open_position_qty = int(status.qty)
        self.state.open_position_entry_price = status.entry_price
        self.state.entered_today = True
        # Bracket IDs are not knowable from broker state alone; the runner
        # will rely on poll_position to detect the close when it happens,
        # and the adapter's internal bracket tracking (if recovered) will
        # do the matching. For now: we know there's a position, but we
        # don't have local bracket bookkeeping for it.
        return status

    def _clear_position(self) -> None:
        self.state.open_position_side = None
        self.state.open_position_qty = 0
        self.state.open_position_entry_price = None
        self.state.open_position_entry_time_utc = None
        self.state.open_position_order_id = None
        self.state.active_bracket_id = None
        self.state.active_tp_order_id = None
        self.state.active_sl_order_id = None
