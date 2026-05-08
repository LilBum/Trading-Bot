import random
from dataclasses import dataclass
from datetime import time
from typing import Iterable

import pytest

from src.futures_execution import (
    BracketAck,
    BracketIntent,
    FuturesOrderAck,
    FuturesOrderIntent,
    FuturesQuote,
    PaperFuturesExecutionAdapter,
    PositionStatus,
)
from src.futures_slippage import FuturesSlippageModel, FuturesSlippageParams


@dataclass
class _StubQuoteProvider:
    """Returns a fixed FuturesQuote per symbol; queue support for sequential tests."""

    quotes: dict[str, FuturesQuote] | None = None
    queue: list[FuturesQuote] | None = None
    raise_on: str | None = None

    def get_quote(self, symbol: str) -> FuturesQuote:
        if self.raise_on and symbol == self.raise_on:
            raise RuntimeError("simulated quote feed error")
        if self.queue:
            return self.queue.pop(0)
        return self.quotes[symbol]


def _adapter(
    seed: int = 0,
    quote_provider: _StubQuoteProvider | None = None,
    et_time_fn=None,
) -> PaperFuturesExecutionAdapter:
    quote_provider = quote_provider or _StubQuoteProvider(
        quotes={
            "ES": FuturesQuote(symbol="ES", bid=4500.00, ask=4500.25, quote_time_utc="2026-05-04T15:00:00+00:00"),
            "NQ": FuturesQuote(symbol="NQ", bid=18000.00, ask=18000.25, quote_time_utc="2026-05-04T15:00:00+00:00"),
        }
    )
    return PaperFuturesExecutionAdapter(
        slippage_model=FuturesSlippageModel(rng=random.Random(seed)),
        quote_provider=quote_provider,
        et_time_fn=et_time_fn or (lambda: time(11, 0)),
    )


def _intent(**overrides) -> FuturesOrderIntent:
    base = dict(symbol="ES", side="BUY", qty=1, order_type="MARKET", intent="entry")
    base.update(overrides)
    return FuturesOrderIntent(**base)


# ----- core fill behavior -----------------------------------------------


def test_buy_market_fills_at_or_above_mid():
    a = _adapter()
    ack = a.submit_order(_intent())
    assert ack.status == "filled"
    assert ack.fill_price is not None
    mid = 0.5 * (4500.00 + 4500.25)
    assert ack.fill_price >= mid - 0.001


def test_sell_market_fills_at_or_below_mid():
    a = _adapter()
    ack = a.submit_order(_intent(side="SELL"))
    assert ack.status == "filled"
    assert ack.fill_price is not None
    mid = 0.5 * (4500.00 + 4500.25)
    assert ack.fill_price <= mid + 0.001


def test_filled_ack_has_required_fields():
    a = _adapter()
    ack = a.submit_order(_intent())
    assert ack.status == "filled"
    assert ack.order_id is not None
    assert ack.filled_qty == 1
    assert ack.fill_price is not None
    assert ack.submission_time_utc is not None
    assert ack.fill_time_utc is not None


def test_unique_order_ids():
    a = _adapter()
    a1 = a.submit_order(_intent())
    a2 = a.submit_order(_intent(side="SELL"))
    assert a1.order_id != a2.order_id


def test_client_order_id_passed_through():
    a = _adapter()
    ack = a.submit_order(_intent(client_order_id="my-order-42"))
    assert ack.order_id == "my-order-42"


# ----- input validation -------------------------------------------------


def test_zero_quantity_rejected():
    a = _adapter()
    ack = a.submit_order(_intent(qty=0))
    assert ack.status == "rejected"
    assert "non-positive" in (ack.reason or "")


def test_negative_quantity_rejected():
    a = _adapter()
    ack = a.submit_order(_intent(qty=-1))
    assert ack.status == "rejected"


def test_limit_order_rejected_in_v1():
    a = _adapter()
    ack = a.submit_order(_intent(order_type="LIMIT", limit_price=4500.00))
    assert ack.status == "rejected"
    assert "MARKET" in (ack.reason or "")


def test_stop_order_rejected_in_v1():
    a = _adapter()
    ack = a.submit_order(_intent(order_type="STOP", stop_price=4490.00))
    assert ack.status == "rejected"


# ----- quote feed errors ------------------------------------------------


def test_quote_provider_error_returns_rejection():
    qp = _StubQuoteProvider(
        quotes={"ES": FuturesQuote(symbol="ES", bid=4500.0, ask=4500.25, quote_time_utc="x")},
        raise_on="ES",
    )
    a = _adapter(quote_provider=qp)
    ack = a.submit_order(_intent())
    assert ack.status == "rejected"
    assert "quote_provider" in (ack.reason or "")


def test_no_quote_returned_returns_rejection():
    """Zero-width spread → slippage model returns no_quote → adapter rejects."""
    qp = _StubQuoteProvider(
        quotes={"ES": FuturesQuote(symbol="ES", bid=4500.0, ask=4500.0, quote_time_utc="x")},
    )
    a = _adapter(quote_provider=qp)
    ack = a.submit_order(_intent())
    assert ack.status == "rejected"


# ----- position tracking ------------------------------------------------


def test_first_fill_creates_open_position():
    a = _adapter()
    a.submit_order(_intent(symbol="ES", side="BUY", qty=2))
    positions = a.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["symbol"] == "ES"
    assert pos["side"] == "BUY"
    assert pos["qty"] == 2


def test_same_side_fill_blends_entry_price():
    a = _adapter()
    a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    positions = a.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["qty"] == 2


def test_opposite_side_close_clears_position_and_reports_pnl():
    a = _adapter()
    open_ack = a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    close_ack = a.submit_order(_intent(symbol="ES", side="SELL", qty=1, intent="tp"))
    assert close_ack.status == "filled"
    assert close_ack.realized_pnl is not None
    assert a.get_open_positions() == []


def test_partial_close_reduces_qty():
    a = _adapter()
    a.submit_order(_intent(symbol="ES", side="BUY", qty=3))
    close_ack = a.submit_order(_intent(symbol="ES", side="SELL", qty=2, intent="tp"))
    assert close_ack.status == "filled"
    positions = a.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["qty"] == 1
    assert positions[0]["side"] == "BUY"


def test_oversized_close_flips_position():
    """Sell 3 contracts when only long 1 → leaves a 2-contract short."""
    a = _adapter()
    a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    a.submit_order(_intent(symbol="ES", side="SELL", qty=3))
    positions = a.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["side"] == "SELL"
    assert positions[0]["qty"] == 2


def test_multiple_symbols_tracked_independently():
    a = _adapter()
    a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    a.submit_order(_intent(symbol="NQ", side="SELL", qty=1))
    positions = a.get_open_positions()
    symbols = {p["symbol"] for p in positions}
    assert symbols == {"ES", "NQ"}


# ----- cancel and reconcile ---------------------------------------------


def test_cancel_returns_cancelled_ack_for_paper():
    a = _adapter()
    ack = a.cancel_order("anything-id")
    assert ack.status == "cancelled"
    assert ack.order_id == "anything-id"


def test_reconcile_reports_state_summary():
    a = _adapter()
    a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    a.submit_order(_intent(symbol="NQ", side="SELL", qty=1))
    summary = a.reconcile()
    assert summary["open_positions"] == 2
    assert summary["order_history_count"] >= 2


# ----- pnl realism vs slippage model ------------------------------------


def test_round_trip_pnl_in_dollars_uses_point_value():
    """Buy ES at one quote, sell at a quote 10 points higher → ~+$500 (10pt × $50)."""
    qp = _StubQuoteProvider(
        queue=[
            FuturesQuote(symbol="ES", bid=4500.00, ask=4500.25, quote_time_utc="t1"),
            FuturesQuote(symbol="ES", bid=4510.00, ask=4510.25, quote_time_utc="t2"),
        ]
    )
    a = _adapter(quote_provider=qp)
    a.submit_order(_intent(symbol="ES", side="BUY", qty=1))
    close = a.submit_order(_intent(symbol="ES", side="SELL", qty=1, intent="tp"))
    assert close.realized_pnl is not None
    # 10 points × $50 = $500 nominal, less round-trip slippage of roughly $15-30.
    assert 460.0 < close.realized_pnl < 510.0


def test_short_round_trip_pnl_correctly_signed():
    """Sell ES at one quote, cover at a quote 10 points lower → ~+$500."""
    qp = _StubQuoteProvider(
        queue=[
            FuturesQuote(symbol="ES", bid=4500.00, ask=4500.25, quote_time_utc="t1"),
            FuturesQuote(symbol="ES", bid=4490.00, ask=4490.25, quote_time_utc="t2"),
        ]
    )
    a = _adapter(quote_provider=qp)
    a.submit_order(_intent(symbol="ES", side="SELL", qty=1))
    close = a.submit_order(_intent(symbol="ES", side="BUY", qty=1, intent="tp"))
    assert close.realized_pnl is not None
    assert 460.0 < close.realized_pnl < 510.0


# ----- Bracket primitives -----------------------------------------------


def _bracket_intent(
    symbol: str = "ES",
    side: str = "BUY",
    qty: int = 1,
    tp_price: float = 4540.0,
    sl_price: float = 4480.0,
    bracket_id: str | None = "test-bracket",
) -> BracketIntent:
    return BracketIntent(
        entry=FuturesOrderIntent(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type="MARKET",
            intent="entry",
        ),
        take_profit_price=tp_price,
        stop_loss_price=sl_price,
        bracket_id=bracket_id,
    )


def test_submit_bracket_fills_entry_and_records_legs():
    a = _adapter()
    ack = a.submit_bracket(_bracket_intent(symbol="ES", side="BUY"))
    assert ack.status == "active"
    assert ack.entry_ack.status == "filled"
    assert ack.take_profit_order_id == "test-bracket-tp"
    assert ack.stop_loss_order_id == "test-bracket-sl"
    # Adapter should now report the position as open.
    status = a.poll_position("ES")
    assert status.state == "open"
    assert status.side == "BUY"
    assert status.qty == 1


def test_submit_bracket_rejects_when_already_active():
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES"))
    second = a.submit_bracket(_bracket_intent(symbol="ES", bracket_id="second"))
    assert second.status == "rejected"
    assert "already active" in (second.reason or "")


def test_poll_position_fires_take_profit_when_reference_hits():
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES", side="BUY", tp_price=4540.0, sl_price=4480.0))
    # First poll: not at TP yet → "open"
    open_status = a.poll_position("ES", reference_price=4530.0)
    assert open_status.state == "open"
    # Second poll: reference >= TP → fires
    tp_status = a.poll_position("ES", reference_price=4541.0)
    assert tp_status.state == "closed_tp"
    assert "TP" in tp_status.note or "tp" in tp_status.note
    # After firing: position should be flat on subsequent polls.
    assert a.poll_position("ES").state == "flat"


def test_poll_position_fires_stop_loss_when_reference_drops():
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES", side="BUY", tp_price=4540.0, sl_price=4480.0))
    sl_status = a.poll_position("ES", reference_price=4475.0)
    assert sl_status.state == "closed_stop"
    assert a.poll_position("ES").state == "flat"


def test_poll_position_short_side_inverted_thresholds():
    a = _adapter()
    a.submit_bracket(_bracket_intent(
        symbol="ES", side="SELL", tp_price=4460.0, sl_price=4520.0,
    ))
    # Reference falling below TP for a short → fires TP
    status = a.poll_position("ES", reference_price=4455.0)
    assert status.state == "closed_tp"


def test_poll_position_short_side_stop_when_price_rises():
    a = _adapter()
    a.submit_bracket(_bracket_intent(
        symbol="ES", side="SELL", tp_price=4460.0, sl_price=4520.0,
    ))
    status = a.poll_position("ES", reference_price=4525.0)
    assert status.state == "closed_stop"


def test_poll_position_when_both_legs_hit_in_same_bar_prefers_stop():
    """Conservative conflict resolution: if reference price has crossed
    BOTH the TP and the stop within the same poll, assume the adverse
    move triggered first."""
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES", side="BUY", tp_price=4520.0, sl_price=4490.0))
    # Reference is in pathological state — pretend bar high hit TP and bar
    # low hit SL. We poll once with a single reference (the close), but
    # if it has crossed BOTH thresholds (impossible from a single close,
    # but the contract is documented), we should pick stop.
    # We simulate this by using a reference that crosses both via state
    # manipulation: temporarily set bracket so both fire.
    # The simpler test: ensure the documented behavior actually picks SL
    # when the reference is below SL even if TP is also crossed.
    a._active_brackets["ES"]["sl_price"] = 4530.0  # now both >4520 (TP) and >=4530 (SL/inverted)
    # With BUY side, sl_hit = ref <= sl. Set ref between TP and SL boundaries.
    # This is contrived but exercises the prefer-stop path.
    status = a.poll_position("ES", reference_price=4521.0)
    # ref >= tp(4520) → tp_hit; ref <= sl(4530) → sl_hit (since BUY: <= sl).
    # Expected: stop preferred.
    assert status.state == "closed_stop"


def test_poll_position_returns_flat_when_no_position():
    a = _adapter()
    assert a.poll_position("ES").state == "flat"
    assert a.poll_position("ES", reference_price=5000.0).state == "flat"


def test_flatten_position_cancels_bracket_and_closes():
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES", side="BUY"))
    assert a.poll_position("ES").state == "open"
    ack = a.flatten_position("ES", reason="session_close")
    assert ack.status == "filled"
    # Bracket should be gone; next poll surfaces the flatten close as
    # closed_other (edge-triggered).
    next_poll = a.poll_position("ES")
    assert next_poll.state == "closed_other"
    assert "session_close" in next_poll.note
    # And then flat afterward.
    assert a.poll_position("ES").state == "flat"


def test_flatten_position_when_no_position_returns_rejected():
    a = _adapter()
    ack = a.flatten_position("ES", reason="session_close")
    assert ack.status == "rejected"
    assert "no open position" in (ack.reason or "")


def test_bracket_supports_independent_symbols():
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES", side="BUY", bracket_id="es-1"))
    a.submit_bracket(_bracket_intent(symbol="NQ", side="SELL", tp_price=17900, sl_price=18100, bracket_id="nq-1"))
    es_status = a.poll_position("ES")
    nq_status = a.poll_position("NQ")
    assert es_status.state == "open"
    assert es_status.side == "BUY"
    assert nq_status.state == "open"
    assert nq_status.side == "SELL"


def test_reconcile_reports_active_brackets():
    a = _adapter()
    a.submit_bracket(_bracket_intent(symbol="ES"))
    summary = a.reconcile()
    assert summary["active_brackets"] == 1
    assert summary["open_positions"] == 1
