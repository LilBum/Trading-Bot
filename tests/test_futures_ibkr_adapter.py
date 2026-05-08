from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.futures_execution import (
    BracketIntent,
    FuturesOrderIntent,
    FuturesQuote,
    PositionStatus,
)
from src.futures_execution.ibkr import (
    DEFAULT_ROLL_DAYS_BEFORE_EXPIRY,
    IBKRConnectionConfig,
    IBKRFuturesExecutionAdapter,
    IBKRFuturesQuoteProvider,
    _parse_expiry,
    resolve_front_month_future,
)


# Fixed "today" used across tests so expiry math is deterministic.
TEST_TODAY = date(2026, 5, 5)


def _today_fn():
    return TEST_TODAY


def _fake_future(symbol: str, expiry: str):
    """Stand-in for ib_insync.Future with the fields our code reads."""
    return SimpleNamespace(
        symbol=symbol,
        exchange="CME",
        currency="USD",
        secType="FUT",
        lastTradeDateOrContractMonth=expiry,
        conId=hash((symbol, expiry)) % 100000,
        localSymbol=f"{symbol}{expiry[-4:]}",
    )


def _fake_contract_details(symbol: str, expiries):
    """Build the list `reqContractDetails` would return."""
    return [SimpleNamespace(contract=_fake_future(symbol, e)) for e in expiries]


def _ib_with_details(symbol: str = "ES", expiries=None):
    """Mock IB client where reqContractDetails returns sane Future details.

    Default `expiries` are intentionally far in the future so existing
    behaviour-focused tests don't break as the calendar advances. Tests
    that exercise roll logic specifically pass their own `expiries`.

    Also wires up `ib.client.getReqId()` since bracket submission
    pre-allocates the parent's orderId before placeOrder.
    """
    if expiries is None:
        expiries = ["20300619", "20300918"]  # far-future quarterly placeholders
    ib = MagicMock()
    ib.reqContractDetails.return_value = _fake_contract_details(symbol, expiries)
    # Monotonically increasing reqIds — tests that need a specific parent
    # orderId can override `ib.client.getReqId.side_effect` after this returns.
    ib.client.getReqId.side_effect = list(range(1000, 2000))
    return ib


# ----- _parse_expiry / resolve_front_month_future -----------------------


def test_parse_expiry_yyyymmdd():
    assert _parse_expiry("20260619") == date(2026, 6, 19)


def test_parse_expiry_yyyymm_uses_day_28():
    # Month-resolution strings should still sort correctly.
    assert _parse_expiry("202606") == date(2026, 6, 28)


def test_parse_expiry_rejects_garbage():
    with pytest.raises(ValueError, match="unparseable"):
        _parse_expiry("not-a-date")


def test_resolve_front_month_picks_earliest_active_expiry():
    ib = _ib_with_details("ES", ["20260918", "20260619", "20261218"])
    cache: dict = {}
    front = resolve_front_month_future(ib, "ES", cache, today_fn=_today_fn)
    # 2026-06-19 is the earliest expiry > 8 days from 2026-05-05 (45 days)
    assert front.lastTradeDateOrContractMonth == "20260619"
    assert cache["ES"] is front


def test_resolve_front_month_skips_contracts_within_roll_window():
    # Cycle where front quarterly is 5 days out (under 8-day roll threshold)
    # → should pick the next quarterly instead.
    ib = _ib_with_details("NQ", ["20260510", "20260918"])
    front = resolve_front_month_future(ib, "NQ", {}, today_fn=_today_fn)
    assert front.lastTradeDateOrContractMonth == "20260918"


def test_resolve_front_month_raises_when_all_expiries_within_window():
    ib = _ib_with_details("ES", ["20260506", "20260510"])  # both too close
    with pytest.raises(RuntimeError, match="No active front-month"):
        resolve_front_month_future(ib, "ES", {}, today_fn=_today_fn)


def test_resolve_front_month_raises_on_unknown_symbol():
    ib = _ib_with_details("ES")
    with pytest.raises(ValueError, match="Unknown"):
        resolve_front_month_future(ib, "ZZZ", {}, today_fn=_today_fn)


def test_resolve_front_month_raises_when_no_details_returned():
    ib = MagicMock()
    ib.reqContractDetails.return_value = []
    with pytest.raises(RuntimeError, match="No contract details"):
        resolve_front_month_future(ib, "ES", {}, today_fn=_today_fn)


def test_resolve_front_month_caches_result():
    ib = _ib_with_details("ES", ["20260619", "20260918"])
    cache: dict = {}
    a = resolve_front_month_future(ib, "ES", cache, today_fn=_today_fn)
    b = resolve_front_month_future(ib, "ES", cache, today_fn=_today_fn)
    assert a is b
    # Second call should not have hit reqContractDetails again.
    assert ib.reqContractDetails.call_count == 1


def test_resolve_front_month_invalidates_cache_at_roll_threshold():
    # Cache an expiring contract; advance "today" so it's now within the
    # roll window. Resolver should re-query and pick the next expiry.
    ib = _ib_with_details("ES", ["20260510", "20260918"])
    cache: dict = {}

    early_today = date(2026, 4, 1)  # 39 days from 2026-05-10 — well outside roll
    a = resolve_front_month_future(ib, "ES", cache, today_fn=lambda: early_today)
    assert a.lastTradeDateOrContractMonth == "20260510"

    late_today = date(2026, 5, 5)   # 5 days from 2026-05-10 — inside roll
    b = resolve_front_month_future(ib, "ES", cache, today_fn=lambda: late_today)
    assert b.lastTradeDateOrContractMonth == "20260918"
    # Cache was invalidated and re-queried.
    assert ib.reqContractDetails.call_count == 2


def test_resolve_front_month_handles_request_exception():
    ib = MagicMock()
    ib.reqContractDetails.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError, match="reqContractDetails failed"):
        resolve_front_month_future(ib, "ES", {}, today_fn=_today_fn)


# Back-compat alias used by the existing tests below — they just need a
# mock IB client where front-month resolution works.
def _ib_with_qualify(symbol: str = "ES"):
    return _ib_with_details(symbol)


def _fake_qualified_contract(symbol: str = "ES"):
    return _fake_future(symbol, "20260619")


def _ticker(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


# ----- IBKRConnectionConfig defaults ------------------------------------


def test_connection_config_paper_gateway_default():
    cfg = IBKRConnectionConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 4002       # paper gateway
    assert cfg.client_id == 1


def test_connection_config_overrides():
    cfg = IBKRConnectionConfig(host="10.0.0.5", port=7497, client_id=42)
    assert cfg.host == "10.0.0.5"
    assert cfg.port == 7497
    assert cfg.client_id == 42


# ----- IBKRFuturesQuoteProvider -----------------------------------------


def test_quote_provider_returns_futures_quote_with_correct_fields():
    ib = _ib_with_qualify("ES")
    ib.reqTickers.return_value = [_ticker(bid=4500.00, ask=4500.25)]
    qp = IBKRFuturesQuoteProvider(ib_client=ib)
    quote = qp.get_quote("ES")
    assert isinstance(quote, FuturesQuote)
    assert quote.symbol == "ES"
    assert quote.bid == 4500.00
    assert quote.ask == 4500.25


def test_quote_provider_caches_qualified_contract():
    ib = _ib_with_qualify("ES")
    ib.reqTickers.return_value = [_ticker(bid=4500.00, ask=4500.25)]
    qp = IBKRFuturesQuoteProvider(ib_client=ib, today_fn=_today_fn)
    qp.get_quote("ES")
    qp.get_quote("ES")
    # reqContractDetails should only be called once thanks to caching.
    assert ib.reqContractDetails.call_count == 1


def test_quote_provider_raises_on_no_tickers():
    ib = _ib_with_qualify("ES")
    ib.reqTickers.return_value = []
    qp = IBKRFuturesQuoteProvider(ib_client=ib)
    with pytest.raises(RuntimeError, match="No tickers"):
        qp.get_quote("ES")


def test_quote_provider_raises_on_invalid_quote():
    ib = _ib_with_qualify("ES")
    ib.reqTickers.return_value = [_ticker(bid=4500.0, ask=4499.0)]  # crossed
    qp = IBKRFuturesQuoteProvider(ib_client=ib)
    with pytest.raises(RuntimeError, match="Invalid"):
        qp.get_quote("ES")


def test_quote_provider_raises_on_unknown_symbol():
    ib = _ib_with_qualify("ZZZ")
    qp = IBKRFuturesQuoteProvider(ib_client=ib)
    with pytest.raises(ValueError, match="Unknown"):
        qp.get_quote("ZZZ")


def test_quote_provider_raises_when_no_contract_details():
    ib = MagicMock()
    ib.reqContractDetails.return_value = []  # IBKR couldn't list any expiries
    qp = IBKRFuturesQuoteProvider(ib_client=ib, today_fn=_today_fn)
    with pytest.raises(RuntimeError, match="No contract details"):
        qp.get_quote("ES")


# ----- IBKRFuturesExecutionAdapter --------------------------------------


def _trade_with_status(status: str, order_id: int = 99, filled: int = 0, avg_fill=None):
    """Construct a stand-in trade as ib_insync's placeOrder would return."""
    order_status = SimpleNamespace(status=status, filled=filled, avgFillPrice=avg_fill)
    order = SimpleNamespace(orderId=order_id)
    return SimpleNamespace(orderStatus=order_status, order=order)


def _intent(**overrides) -> FuturesOrderIntent:
    base = dict(symbol="ES", side="BUY", qty=1, order_type="MARKET", intent="entry")
    base.update(overrides)
    return FuturesOrderIntent(**base)


def test_adapter_rejects_non_positive_qty():
    ib = _ib_with_qualify("ES")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent(qty=0))
    assert ack.status == "rejected"
    assert "non-positive" in (ack.reason or "")


def test_adapter_rejects_unknown_symbol():
    ib = _ib_with_qualify("ZZZ")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent(symbol="ZZZ"))
    assert ack.status == "rejected"
    assert "contract resolution" in (ack.reason or "").lower()


def test_adapter_rejects_unsupported_order_type():
    ib = _ib_with_qualify("ES")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent(order_type="STOP_LIMIT", limit_price=4500.0, stop_price=4495.0))
    assert ack.status == "rejected"
    assert "unsupported order_type" in (ack.reason or "").lower()


def test_adapter_limit_order_requires_limit_price():
    ib = _ib_with_qualify("ES")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent(order_type="LIMIT", limit_price=None))
    assert ack.status == "rejected"
    assert "limit_price" in (ack.reason or "").lower()


def test_adapter_stop_order_requires_stop_price():
    ib = _ib_with_qualify("ES")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent(order_type="STOP", stop_price=None))
    assert ack.status == "rejected"
    assert "stop_price" in (ack.reason or "").lower()


def test_adapter_market_order_filled_returns_filled_ack():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.return_value = _trade_with_status(
        "Filled", order_id=42, filled=1, avg_fill=4500.25
    )
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent())
    assert ack.status == "filled"
    assert ack.order_id == "42"
    assert ack.filled_qty == 1
    assert ack.fill_price == 4500.25


def test_adapter_market_order_pending_returns_submitted_ack():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.return_value = _trade_with_status("PreSubmitted", order_id=43)
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent())
    assert ack.status == "submitted"
    assert ack.order_id == "43"


def test_adapter_market_order_cancelled_returns_cancelled_ack():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.return_value = _trade_with_status("Cancelled", order_id=44)
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent())
    assert ack.status == "cancelled"


def test_adapter_handles_place_order_exception():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.side_effect = RuntimeError("network down")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.submit_order(_intent())
    assert ack.status == "rejected"
    assert "placeOrder" in (ack.reason or "")


def test_adapter_passes_account_through_when_provided():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.return_value = _trade_with_status("Filled", filled=1, avg_fill=4500.25)
    a = IBKRFuturesExecutionAdapter(ib_client=ib, account="DU1234567")
    a.submit_order(_intent())
    submitted_order = ib.placeOrder.call_args[0][1]
    assert submitted_order.account == "DU1234567"


def test_adapter_sets_orderref_to_client_order_id():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.return_value = _trade_with_status("Filled", filled=1, avg_fill=4500.25)
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    a.submit_order(_intent(client_order_id="my-orb-trade-1"))
    submitted_order = ib.placeOrder.call_args[0][1]
    assert submitted_order.orderRef == "my-orb-trade-1"


# ----- cancel_order -----------------------------------------------------


def test_cancel_order_finds_trade_and_cancels():
    ib = MagicMock()
    target_order = SimpleNamespace(orderId=42)
    open_trade = SimpleNamespace(order=target_order)
    ib.openTrades.return_value = [open_trade]
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.cancel_order("42")
    assert ack.status == "cancelled"
    assert ack.order_id == "42"
    ib.cancelOrder.assert_called_once_with(target_order)


def test_cancel_order_rejects_when_not_found():
    ib = MagicMock()
    ib.openTrades.return_value = [SimpleNamespace(order=SimpleNamespace(orderId=99))]
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.cancel_order("42")
    assert ack.status == "rejected"
    assert "not found" in (ack.reason or "").lower()


# ----- get_open_positions / reconcile -----------------------------------


def test_get_open_positions_filters_zero_positions():
    ib = MagicMock()
    long_pos = SimpleNamespace(
        contract=SimpleNamespace(symbol="ES"), position=2.0, avgCost=4500.0, account="DU1"
    )
    flat_pos = SimpleNamespace(
        contract=SimpleNamespace(symbol="NQ"), position=0.0, avgCost=18000.0, account="DU1"
    )
    short_pos = SimpleNamespace(
        contract=SimpleNamespace(symbol="MNQ"), position=-1.0, avgCost=18050.0, account="DU1"
    )
    ib.positions.return_value = [long_pos, flat_pos, short_pos]
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    positions = a.get_open_positions()
    assert len(positions) == 2
    es = next(p for p in positions if p["symbol"] == "ES")
    assert es["side"] == "BUY"
    assert es["qty"] == 2
    mnq = next(p for p in positions if p["symbol"] == "MNQ")
    assert mnq["side"] == "SELL"
    assert mnq["qty"] == 1


def test_get_open_positions_returns_empty_on_error():
    ib = MagicMock()
    ib.positions.side_effect = RuntimeError("disconnected")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    assert a.get_open_positions() == []


def test_reconcile_reports_state_summary():
    ib = MagicMock()
    ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="ES"),
            position=1.0, avgCost=4500.0, account="DU1",
        )
    ]
    ib.openTrades.return_value = [MagicMock(), MagicMock()]
    ib.isConnected.return_value = True
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    summary = a.reconcile()
    assert summary["open_positions"] == 1
    assert summary["open_trades"] == 2
    assert summary["connected"] is True
    assert "active_brackets" in summary


# ----- IBKR bracket primitives ------------------------------------------


def _bracket_intent(
    symbol: str = "ES",
    side: str = "BUY",
    qty: int = 1,
    tp_price: float = 4540.0,
    sl_price: float = 4480.0,
    bracket_id: str = "test-bracket",
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


def _trade_with_id(order_id: int, status: str = "Submitted", filled: int = 0, avg_fill=None):
    """Stand-in for ib_insync.Trade with specific orderId."""
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=avg_fill),
    )


def test_submit_bracket_places_three_orders_with_oca_linkage():
    ib = _ib_with_qualify("ES")
    # Pin the parent's pre-allocated orderId so the test can verify children
    # were tagged with it BEFORE placeOrder.
    ib.client.getReqId.side_effect = [555]
    ib.placeOrder.side_effect = [
        _trade_with_id(555, "Filled", filled=1, avg_fill=4500.5),  # parent
        _trade_with_id(556, "Submitted"),                            # tp child
        _trade_with_id(557, "Submitted"),                            # sl child
    ]
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    ack = a.submit_bracket(_bracket_intent(symbol="ES", tp_price=4540.0, sl_price=4480.0))
    assert ack.status == "active"
    assert ack.entry_ack.status == "filled"
    # Three placeOrder calls: parent, TP, SL.
    assert ib.placeOrder.call_count == 3
    # Verify OCA group is consistent across the children and TP/SL prices.
    parent_order = ib.placeOrder.call_args_list[0][0][1]
    tp_order = ib.placeOrder.call_args_list[1][0][1]
    sl_order = ib.placeOrder.call_args_list[2][0][1]
    assert tp_order.ocaGroup == sl_order.ocaGroup
    assert tp_order.ocaGroup == "test-bracket"
    # TP transmits=False, SL transmits=True so all three reach broker atomically.
    assert parent_order.transmit is False
    assert tp_order.transmit is False
    assert sl_order.transmit is True
    # Critical: parentId must be set on children BEFORE placeOrder, so the
    # broker links them as parent-child on receipt rather than treating them
    # as independent orders.
    assert parent_order.orderId == 555
    assert tp_order.parentId == 555
    assert sl_order.parentId == 555


def test_submit_bracket_handles_getreqid_failure():
    """If we can't pre-allocate the parent's orderId, the bracket is rejected
    rather than submitted with mis-linked children."""
    ib = _ib_with_qualify("ES")
    ib.client.getReqId.side_effect = RuntimeError("client not connected")
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    ack = a.submit_bracket(_bracket_intent(symbol="ES"))
    assert ack.status == "rejected"
    assert "orderId" in (ack.reason or "")
    # No placeOrder call should have been attempted.
    assert ib.placeOrder.call_count == 0


def test_submit_bracket_rejects_when_already_active():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.side_effect = [
        _trade_with_id(201, "Filled", filled=1, avg_fill=4500.0),
        _trade_with_id(202, "Submitted"),
        _trade_with_id(203, "Submitted"),
    ]
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    a.submit_bracket(_bracket_intent(symbol="ES"))
    second = a.submit_bracket(_bracket_intent(symbol="ES", bracket_id="second"))
    assert second.status == "rejected"
    assert "already active" in (second.reason or "")


def test_submit_bracket_rejects_non_market_parent():
    ib = _ib_with_qualify("ES")
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    intent = BracketIntent(
        entry=FuturesOrderIntent(
            symbol="ES", side="BUY", qty=1, order_type="LIMIT",
            limit_price=4500.0, intent="entry",
        ),
        take_profit_price=4540.0,
        stop_loss_price=4480.0,
    )
    ack = a.submit_bracket(intent)
    assert ack.status == "rejected"
    assert "MARKET" in (ack.reason or "")


def test_submit_bracket_rejects_non_positive_qty():
    ib = _ib_with_qualify("ES")
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    ack = a.submit_bracket(_bracket_intent(qty=0))
    assert ack.status == "rejected"
    assert "non-positive" in (ack.reason or "")


def test_submit_bracket_handles_place_order_exception():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.side_effect = RuntimeError("network blip")
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    ack = a.submit_bracket(_bracket_intent())
    assert ack.status == "rejected"
    assert "placeOrder" in (ack.reason or "") or "network blip" in (ack.reason or "")


def test_poll_position_reports_open_when_broker_has_position():
    ib = MagicMock()
    ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="ES"),
            position=2.0, avgCost=4501.5, account="DU1",
        )
    ]
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    status = a.poll_position("ES")
    assert status.state == "open"
    assert status.side == "BUY"
    assert status.qty == 2
    assert status.entry_price == 4501.5


def test_poll_position_reports_flat_when_no_position():
    ib = MagicMock()
    ib.positions.return_value = []
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    assert a.poll_position("ES").state == "flat"


def test_poll_position_returns_unknown_on_positions_query_failure():
    """Transient broker query failure must surface as 'unknown', NOT 'flat'.
    The runner relies on this distinction to preserve local state across
    network blips instead of dropping tracking of a real open position."""
    ib = MagicMock()
    ib.positions.side_effect = RuntimeError("connection reset")
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    status = a.poll_position("ES")
    assert status.state == "unknown"
    assert "connection reset" in status.note


def test_poll_position_detects_tp_fill_when_position_closes():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.side_effect = [
        _trade_with_id(301, "Filled", filled=1, avg_fill=4500.0),  # parent
        _trade_with_id(302, "Submitted"),                            # tp child
        _trade_with_id(303, "Submitted"),                            # sl child
    ]
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    a.submit_bracket(_bracket_intent(symbol="ES"))

    # Now broker shows position closed and fills include the TP child fill.
    ib.positions.return_value = [
        SimpleNamespace(contract=SimpleNamespace(symbol="ES"), position=0.0, avgCost=0)
    ]
    ib.fills.return_value = [
        SimpleNamespace(execution=SimpleNamespace(
            execId="exec-1", orderId=302, price=4540.0,
        ))
    ]
    status = a.poll_position("ES")
    assert status.state == "closed_tp"
    assert status.last_fill_price == 4540.0
    # Subsequent polls return flat — edge-triggered close.
    ib.fills.return_value = []
    assert a.poll_position("ES").state == "flat"


def test_poll_position_detects_stop_fill_when_position_closes():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.side_effect = [
        _trade_with_id(401, "Filled", filled=1, avg_fill=4500.0),
        _trade_with_id(402, "Submitted"),
        _trade_with_id(403, "Submitted"),
    ]
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    a.submit_bracket(_bracket_intent(symbol="ES"))

    ib.positions.return_value = []
    ib.fills.return_value = [
        SimpleNamespace(execution=SimpleNamespace(
            execId="exec-2", orderId=403, price=4480.0,
        ))
    ]
    status = a.poll_position("ES")
    assert status.state == "closed_stop"
    assert status.last_fill_price == 4480.0


def test_flatten_position_cancels_bracket_children_then_offsets():
    ib = _ib_with_qualify("ES")
    ib.placeOrder.side_effect = [
        _trade_with_id(501, "Filled", filled=1, avg_fill=4500.0),  # parent
        _trade_with_id(502, "Submitted"),                            # tp
        _trade_with_id(503, "Submitted"),                            # sl
        _trade_with_id(504, "Filled", filled=1, avg_fill=4502.0),   # offset
    ]
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    a.submit_bracket(_bracket_intent(symbol="ES"))

    # Live broker state: position still open, TP and SL children still working.
    ib.positions.return_value = [
        SimpleNamespace(contract=SimpleNamespace(symbol="ES"), position=1.0, avgCost=4500.0, account="DU1")
    ]
    es_contract = SimpleNamespace(symbol="ES")
    tp_trade = SimpleNamespace(order=SimpleNamespace(orderId=502), contract=es_contract)
    sl_trade = SimpleNamespace(order=SimpleNamespace(orderId=503), contract=es_contract)
    ib.openTrades.return_value = [tp_trade, sl_trade]

    ack = a.flatten_position("ES", reason="session_close")
    assert ack.status == "filled"
    # Both children should have been cancelled before the offsetting MARKET.
    cancelled = [c.args[0] for c in ib.cancelOrder.call_args_list]
    assert tp_trade.order in cancelled
    assert sl_trade.order in cancelled


def test_flatten_position_returns_rejected_when_no_position():
    ib = MagicMock()
    ib.positions.return_value = []
    a = IBKRFuturesExecutionAdapter(ib_client=ib)
    ack = a.flatten_position("ES", reason="session_close")
    assert ack.status == "rejected"
    assert "no open position" in (ack.reason or "")


def test_flatten_position_cancels_orders_on_symbol_even_when_not_tracked():
    """After reconcile-on-start, recovered positions have no internal bracket
    bookkeeping. flatten_position must still cancel ANY working orders on
    the symbol so they don't leave stale TP/SL legs behind."""
    ib = _ib_with_qualify("ES")
    # Adapter has no _active_brackets entry - simulating recovery from a
    # previous process run.
    ib.positions.return_value = [
        SimpleNamespace(contract=SimpleNamespace(symbol="ES"), position=1.0,
                        avgCost=4500.0, account="DU1")
    ]
    # Two stale working orders on this symbol from before the restart, plus
    # one on a different symbol that should NOT be touched.
    es_contract = SimpleNamespace(symbol="ES")
    stale_tp = SimpleNamespace(
        order=SimpleNamespace(orderId=801),
        contract=es_contract,
    )
    stale_sl = SimpleNamespace(
        order=SimpleNamespace(orderId=802),
        contract=es_contract,
    )
    other_symbol_order = SimpleNamespace(
        order=SimpleNamespace(orderId=900),
        contract=SimpleNamespace(symbol="NQ"),
    )
    ib.openTrades.return_value = [stale_tp, stale_sl, other_symbol_order]
    # Offsetting MARKET succeeds.
    ib.placeOrder.return_value = _trade_with_id(803, "Filled", filled=1, avg_fill=4501.0)
    a = IBKRFuturesExecutionAdapter(ib_client=ib, today_fn=_today_fn)
    ack = a.flatten_position("ES", reason="session_close")
    assert ack.status == "filled"
    cancelled_orders = [c.args[0] for c in ib.cancelOrder.call_args_list]
    # Both ES working orders should be cancelled.
    assert stale_tp.order in cancelled_orders
    assert stale_sl.order in cancelled_orders
    # NQ order on a different symbol must NOT be touched.
    assert other_symbol_order.order not in cancelled_orders
