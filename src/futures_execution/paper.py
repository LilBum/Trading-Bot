"""Pure internal paper-trading adapter for futures.

Uses the same FuturesSlippageModel as the backtest harness. If the strategy
behaves differently in this paper mode than in the backtest, that's a sign
the harness is mis-specified — paper PnL should track backtest PnL within
sampling noise.

Limitations of this v1:
- MARKET orders only. LIMIT/STOP support deferred until we have a real-feed
  quote stream and a fill-supervision loop.
- Single position per symbol (no scaling in/out).
- No working-order queue; all orders attempt immediate fill.
- No inter-process persistence; if the process restarts, in-memory state is
  lost. Persisting positions to disk is a Phase 4 follow-up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

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
from src.futures_slippage import (
    CONTRACTS,
    FuturesContract,
    FuturesFillRequest,
    FuturesSlippageModel,
)


EASTERN = ZoneInfo("America/New_York")


@dataclass
class PaperPositionRecord:
    """Open paper position. Mutable so we can update on partial fills/exits."""

    symbol: str
    side: str               # "BUY" or "SELL"
    qty: int
    entry_price: float
    entry_time_utc: str
    point_value: float


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _et_time_now() -> time:
    return datetime.now(EASTERN).time()


class PaperFuturesExecutionAdapter(FuturesExecutionAdapter):
    """Internal-paper adapter: simulates fills via the slippage model."""

    def __init__(
        self,
        slippage_model: FuturesSlippageModel,
        quote_provider: FuturesQuoteProvider,
        underlying_sigma_by_symbol: dict[str, float] | None = None,
        contract_specs: dict[str, FuturesContract] | None = None,
        et_time_fn=None,
    ) -> None:
        self.slippage_model = slippage_model
        self.quote_provider = quote_provider
        self.underlying_sigma_by_symbol = underlying_sigma_by_symbol or {
            "ES": 0.18, "NQ": 0.22, "MES": 0.18, "MNQ": 0.22,
        }
        self.contract_specs = dict(contract_specs) if contract_specs else dict(CONTRACTS)
        self._positions: dict[str, PaperPositionRecord] = {}
        self._order_history: list[FuturesOrderAck] = []
        self._et_time_fn = et_time_fn or _et_time_now
        # Active brackets keyed by symbol. Cleared when a leg fires or the
        # position is flattened.
        self._active_brackets: dict[str, dict] = {}
        # Edge-triggered close events that need to surface on the next
        # poll_position call. Cleared after read.
        self._pending_close: dict[str, PositionStatus] = {}

    def submit_order(self, intent: FuturesOrderIntent) -> FuturesOrderAck:
        if intent.qty <= 0:
            return self._record(FuturesOrderAck(
                status="rejected",
                reason="non-positive quantity",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            ))

        order_type = intent.order_type.upper()
        if order_type != "MARKET":
            return self._record(FuturesOrderAck(
                status="rejected",
                reason=f"order_type {order_type} not supported in paper v1; use MARKET",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            ))

        try:
            quote = self.quote_provider.get_quote(intent.symbol)
        except Exception as exc:
            return self._record(FuturesOrderAck(
                status="rejected",
                reason=f"quote_provider error: {exc}",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            ))

        sigma_ann = float(self.underlying_sigma_by_symbol.get(intent.symbol, 0.20))
        spec = self.contract_specs.get(intent.symbol) or FuturesContract(
            tick_size=0.25, point_value=50.0
        )

        fill_request = FuturesFillRequest(
            side=intent.side,
            intent=intent.intent,
            bid=quote.bid,
            ask=quote.ask,
            underlying_sigma_ann=sigma_ann,
            quote_age_ms=200,
            decision_to_submit_ms=300,
            submit_to_fill_ms=200,
            now_local_time=self._et_time_fn(),
            symbol=intent.symbol,
            qty=intent.qty,
            order_type="market",
        )
        fill = self.slippage_model.estimate_fill(fill_request)
        order_id = intent.client_order_id or f"paper-{uuid.uuid4().hex[:12]}"

        if fill.fill_price is None:
            return self._record(FuturesOrderAck(
                status="rejected",
                reason=f"slippage model returned no fill: {fill.status}",
                submission_time_utc=_utc_now_iso(),
                order_id=order_id,
            ))

        realized_pnl = self._apply_fill(intent, fill.fill_price, spec)
        ack_time = _utc_now_iso()
        return self._record(FuturesOrderAck(
            status="filled",
            order_id=order_id,
            filled_qty=intent.qty,
            fill_price=fill.fill_price,
            reason=None,
            submission_time_utc=ack_time,
            fill_time_utc=ack_time,
            realized_pnl=realized_pnl,
        ))

    def cancel_order(self, order_id: str) -> FuturesOrderAck:
        # Paper v1 fills immediately; nothing to cancel. Acknowledge no-op.
        return self._record(FuturesOrderAck(
            status="cancelled",
            order_id=order_id,
            reason="paper v1 fills synchronously; no working orders to cancel",
            submission_time_utc=_utc_now_iso(),
        ))

    def get_open_positions(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "qty": p.qty,
                "entry_price": p.entry_price,
                "entry_time_utc": p.entry_time_utc,
                "point_value": p.point_value,
            }
            for p in self._positions.values()
        ]

    def reconcile(self) -> dict:
        return {
            "open_positions": len(self._positions),
            "order_history_count": len(self._order_history),
            "active_brackets": len(self._active_brackets),
        }

    # ---- Bracket primitives ---------------------------------------------

    def submit_bracket(self, intent: BracketIntent) -> BracketAck:
        """Submit entry as a paper MARKET fill, then attach virtual OCO legs.

        The TP and SL are simulated client-side: `poll_position` checks the
        reference price each call and fires the appropriate leg when crossed.
        """
        symbol = intent.entry.symbol
        # Reject if there's an existing bracket; one position per symbol in v1.
        if symbol in self._active_brackets:
            return BracketAck(
                status="rejected",
                entry_ack=self._record(FuturesOrderAck(
                    status="rejected",
                    reason="bracket already active for symbol",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.entry.client_order_id,
                )),
                bracket_id=intent.bracket_id,
                reason="bracket already active for symbol",
            )

        entry_ack = self.submit_order(intent.entry)
        if entry_ack.status != "filled":
            return BracketAck(
                status="rejected",
                entry_ack=entry_ack,
                bracket_id=intent.bracket_id,
                reason=f"entry not filled: {entry_ack.status}",
            )

        bracket_id = intent.bracket_id or f"bracket-{uuid.uuid4().hex[:12]}"
        tp_id = f"{bracket_id}-tp"
        sl_id = f"{bracket_id}-sl"
        self._active_brackets[symbol] = {
            "bracket_id": bracket_id,
            "tp_price": float(intent.take_profit_price),
            "sl_price": float(intent.stop_loss_price),
            "tp_order_id": tp_id,
            "sl_order_id": sl_id,
            "side": intent.entry.side,
            "qty": int(intent.entry.qty),
        }
        return BracketAck(
            status="active",
            entry_ack=entry_ack,
            take_profit_order_id=tp_id,
            stop_loss_order_id=sl_id,
            bracket_id=bracket_id,
        )

    def poll_position(
        self,
        symbol: str,
        *,
        reference_price: Optional[float] = None,
    ) -> PositionStatus:
        # Edge-triggered close: surface once, then clear.
        pending = self._pending_close.pop(symbol, None)
        if pending is not None:
            return pending

        pos = self._positions.get(symbol)
        if pos is None:
            return PositionStatus(state="flat")

        # Position open. If we have a virtual bracket and a reference price,
        # check whether either leg should fire this call.
        bracket = self._active_brackets.get(symbol)
        if bracket is not None and reference_price is not None:
            ref = float(reference_price)
            side = bracket["side"]
            tp = bracket["tp_price"]
            sl = bracket["sl_price"]
            tp_hit = (side == "BUY" and ref >= tp) or (side == "SELL" and ref <= tp)
            sl_hit = (side == "BUY" and ref <= sl) or (side == "SELL" and ref >= sl)

            # If both fire same bar, prefer SL (conservative — assume the
            # adverse move triggered before the favourable one).
            if sl_hit:
                return self._fire_bracket_leg(symbol, "stop", bracket, ref)
            if tp_hit:
                return self._fire_bracket_leg(symbol, "tp", bracket, ref)

        return PositionStatus(
            state="open",
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
        )

    def flatten_position(
        self,
        symbol: str,
        *,
        reason: str = "",
    ) -> FuturesOrderAck:
        """Cancel any active bracket and submit an offsetting MARKET."""
        pos = self._positions.get(symbol)
        if pos is None:
            self._active_brackets.pop(symbol, None)
            return self._record(FuturesOrderAck(
                status="rejected",
                reason="no open position to flatten",
                submission_time_utc=_utc_now_iso(),
            ))

        # Drop the virtual bracket so the offsetting fill below isn't
        # double-resolved by poll_position.
        self._active_brackets.pop(symbol, None)
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        flatten_intent = FuturesOrderIntent(
            symbol=symbol,
            side=close_side,
            qty=pos.qty,
            order_type="MARKET",
            intent="time_stop",
        )
        ack = self.submit_order(flatten_intent)
        # Surface the close as an edge-triggered status on next poll.
        if ack.status == "filled":
            self._pending_close[symbol] = PositionStatus(
                state="closed_other",
                last_fill_price=ack.fill_price,
                realized_pnl=ack.realized_pnl,
                note=reason or "flatten",
            )
        return ack

    def _fire_bracket_leg(
        self,
        symbol: str,
        leg: str,                 # "tp" | "stop"
        bracket: dict,
        reference_price: float,
    ) -> PositionStatus:
        """Resolve the position via the slippage model at `reference_price`.

        Returns the closed-state PositionStatus directly (caller is the
        first poll after the leg fires; edge-triggered semantics).
        """
        close_side = "SELL" if bracket["side"] == "BUY" else "BUY"
        intent_label = "tp" if leg == "tp" else "stop"
        close_intent = FuturesOrderIntent(
            symbol=symbol,
            side=close_side,
            qty=bracket["qty"],
            order_type="MARKET",
            intent=intent_label,
        )
        ack = self.submit_order(close_intent)
        # Bracket is consumed regardless of fill outcome.
        self._active_brackets.pop(symbol, None)

        state = "closed_tp" if leg == "tp" else "closed_stop"
        return PositionStatus(
            state=state,
            last_fill_price=ack.fill_price,
            realized_pnl=ack.realized_pnl,
            note=f"bracket {leg} fired at ref={reference_price:.2f}",
        )

    def _apply_fill(
        self,
        intent: FuturesOrderIntent,
        fill_price: float,
        spec: FuturesContract,
    ) -> Optional[float]:
        """Update in-memory position state. Returns realized PnL on closes."""
        existing = self._positions.get(intent.symbol)

        if existing is None:
            self._positions[intent.symbol] = PaperPositionRecord(
                symbol=intent.symbol,
                side=intent.side,
                qty=intent.qty,
                entry_price=fill_price,
                entry_time_utc=_utc_now_iso(),
                point_value=spec.point_value,
            )
            return None

        if existing.side == intent.side:
            new_qty = existing.qty + intent.qty
            blended = (
                existing.entry_price * existing.qty + fill_price * intent.qty
            ) / new_qty
            self._positions[intent.symbol] = replace(
                existing, qty=new_qty, entry_price=blended
            )
            return None

        # Opposite-side trade: closing or reducing.
        closing_qty = min(existing.qty, intent.qty)
        direction = +1 if existing.side == "BUY" else -1
        realized = (
            direction * (fill_price - existing.entry_price)
            * closing_qty * existing.point_value
        )
        remaining = existing.qty - intent.qty
        if remaining == 0:
            del self._positions[intent.symbol]
        elif remaining > 0:
            self._positions[intent.symbol] = replace(existing, qty=remaining)
        else:
            # Flip: net opposite position remaining.
            self._positions[intent.symbol] = PaperPositionRecord(
                symbol=intent.symbol,
                side=intent.side,
                qty=-remaining,
                entry_price=fill_price,
                entry_time_utc=_utc_now_iso(),
                point_value=existing.point_value,
            )
        return realized

    def _record(self, ack: FuturesOrderAck) -> FuturesOrderAck:
        self._order_history.append(ack)
        return ack
