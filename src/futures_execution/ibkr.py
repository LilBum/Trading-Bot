"""IBKR futures execution + quote provider via the TWS API (ib_insync).

Connects to a running IB Gateway or TWS instance. Default ports:
- Paper Gateway: 4002    Live Gateway: 4001
- Paper TWS:     7497    Live TWS:     7496

Both account types use the same API surface; only the port and account ID
differ. The adapter does NOT manage the Gateway lifecycle — caller is
responsible for `connect()` / `disconnect()` and for keeping Gateway up
during the trading window.

**Contract resolution: front-month `Future`, not `ContFuture`.**
IBKR documents `ContFuture` as historical-data-only — it cannot be used
for `placeOrder` or live `reqMktData` / `reqTickers`. We resolve the active
front-month real `Future` contract via `reqContractDetails` and re-resolve
on roll. `ContFuture` remains in `ibkr_bars.py` for `reqHistoricalData`,
which is its supported use.
"""

from __future__ import annotations

import asyncio as _asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional

import uuid

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

# ib_insync (via eventkit) touches asyncio at import time; on Python 3.12+
# `asyncio.get_event_loop()` raises if no loop is set. Prime one before
# importing so module load succeeds in any context (including pytest collect).
try:
    _asyncio.get_event_loop()
except RuntimeError:
    _asyncio.set_event_loop(_asyncio.new_event_loop())

try:  # pragma: no cover - optional runtime dependency
    from ib_insync import (
        IB,
        Future,
        LimitOrder,
        MarketOrder,
        Order,
        StopOrder,
    )
except ImportError:  # pragma: no cover
    IB = Future = LimitOrder = MarketOrder = Order = StopOrder = None


# Default number of days before contract expiry at which we roll to the
# next front month. CME quarterly index futures (ES/NQ) typically see
# volume migrate to the next quarter ~8 trading days before expiry.
DEFAULT_ROLL_DAYS_BEFORE_EXPIRY = 8


# Default contract spec per symbol. CME for index futures; currency USD.
IBKR_CONTRACT_SPEC: dict[str, dict[str, str]] = {
    "ES":  {"exchange": "CME", "currency": "USD"},
    "NQ":  {"exchange": "CME", "currency": "USD"},
    "MES": {"exchange": "CME", "currency": "USD"},
    "MNQ": {"exchange": "CME", "currency": "USD"},
    "YM":  {"exchange": "CBOT", "currency": "USD"},
    "RTY": {"exchange": "CME", "currency": "USD"},
}


@dataclass(frozen=True)
class IBKRConnectionConfig:
    """How to reach IB Gateway / TWS."""

    host: str = "127.0.0.1"
    port: int = 4002             # paper Gateway default
    client_id: int = 1
    account: Optional[str] = None  # required only for multi-account logins
    connect_timeout_seconds: float = 10.0
    fill_wait_seconds: float = 5.0   # how long to wait for synchronous fill ack


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_expiry(expiry_str: str) -> date:
    """Parse an IBKR `lastTradeDateOrContractMonth` string.

    Common formats from `reqContractDetails`:
      - 'YYYYMMDD' (8 chars) — daily resolution
      - 'YYYYMM'   (6 chars) — month resolution

    For month-resolution strings we use day=28 as a within-month proxy for
    ordering purposes; we never settle/exercise based on this date so the
    proxy is safe.
    """
    s = str(expiry_str).strip()
    if len(s) == 8:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    if len(s) == 6:
        return date(int(s[0:4]), int(s[4:6]), 28)
    raise ValueError(f"unparseable IBKR expiry: {expiry_str!r}")


def resolve_front_month_future(
    ib_client,
    symbol: str,
    contract_cache: dict,
    *,
    roll_days_before_expiry: int = DEFAULT_ROLL_DAYS_BEFORE_EXPIRY,
    today_fn: Optional[Callable[[], date]] = None,
) -> object:
    """Resolve the active front-month `Future` for `symbol`.

    Strategy:
      1. If we have a cached contract whose expiry is still > N days out,
         return it.
      2. Otherwise, call `reqContractDetails` with a no-expiry `Future`
         template; IBKR returns all listed expiries.
      3. Filter to expiries at least `roll_days_before_expiry` days away —
         this approximates the typical CME index-futures roll window.
      4. Pick the earliest remaining expiry — that's the active front month.
      5. Cache by symbol; cached contract is invalidated automatically as
         it approaches the roll threshold.

    `today_fn` is injectable for deterministic testing (default: today UTC).

    Replaces the previous ContFuture-based resolver because IBKR documents
    `ContFuture` as historical-data-only — it cannot back live order
    placement or live quote subscriptions.
    """
    today = today_fn() if today_fn else datetime.now(timezone.utc).date()

    cached = contract_cache.get(symbol)
    if cached is not None:
        try:
            cached_expiry = _parse_expiry(
                getattr(cached, "lastTradeDateOrContractMonth", "")
            )
            if (cached_expiry - today).days >= roll_days_before_expiry:
                return cached
        except (ValueError, TypeError):
            pass  # malformed cache entry — fall through and re-resolve
        contract_cache.pop(symbol, None)

    if Future is None:
        raise RuntimeError("ib_insync not installed; cannot resolve IBKR contracts.")
    spec = IBKR_CONTRACT_SPEC.get(symbol)
    if spec is None:
        raise ValueError(f"Unknown IBKR futures symbol: {symbol}")

    template = Future(symbol, exchange=spec["exchange"], currency=spec["currency"])
    try:
        details = ib_client.reqContractDetails(template)
    except Exception as exc:
        raise RuntimeError(f"reqContractDetails failed for {symbol}: {exc}") from exc
    if not details:
        raise RuntimeError(f"No contract details returned for {symbol}")

    candidates: list[tuple[date, object]] = []
    for d in details:
        contract = getattr(d, "contract", None)
        if contract is None:
            continue
        expiry_str = getattr(contract, "lastTradeDateOrContractMonth", "")
        try:
            expiry_date = _parse_expiry(expiry_str)
        except ValueError:
            continue
        if (expiry_date - today).days >= roll_days_before_expiry:
            candidates.append((expiry_date, contract))

    if not candidates:
        raise RuntimeError(
            f"No active front-month contract for {symbol} "
            f"(all listed expiries are within {roll_days_before_expiry} days)"
        )

    candidates.sort(key=lambda t: t[0])
    front = candidates[0][1]
    contract_cache[symbol] = front
    return front


class IBKRFuturesQuoteProvider(FuturesQuoteProvider):
    """Snapshot quote provider backed by IBKR `reqTickers`.

    Uses one-shot snapshots rather than streaming subscriptions — appropriate
    for our once-per-bar polling pattern. If we move to higher-frequency
    decisioning we'll switch to `reqMktData` and cache.

    Resolves the active front-month `Future` (not `ContFuture`) since
    `reqTickers` is a live-data call and ContFuture is historical-only.
    """

    def __init__(
        self,
        ib_client,
        *,
        roll_days_before_expiry: int = DEFAULT_ROLL_DAYS_BEFORE_EXPIRY,
        today_fn: Optional[Callable[[], date]] = None,
    ) -> None:
        self._ib = ib_client
        self._contract_cache: dict = {}
        self._roll_days = int(roll_days_before_expiry)
        self._today_fn = today_fn

    def get_quote(self, symbol: str) -> FuturesQuote:
        contract = resolve_front_month_future(
            self._ib,
            symbol,
            self._contract_cache,
            roll_days_before_expiry=self._roll_days,
            today_fn=self._today_fn,
        )
        tickers = self._ib.reqTickers(contract)
        if not tickers:
            raise RuntimeError(f"No tickers returned for {symbol}")
        ticker = tickers[0]
        bid = float(ticker.bid) if ticker.bid is not None else 0.0
        ask = float(ticker.ask) if ticker.ask is not None else 0.0
        if bid <= 0 or ask <= 0 or ask <= bid:
            raise RuntimeError(f"Invalid {symbol} quote: bid={bid}, ask={ask}")
        return FuturesQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            quote_time_utc=_utc_now_iso(),
        )


class IBKRFuturesExecutionAdapter(FuturesExecutionAdapter):
    """Live-or-paper IBKR futures execution via ib_insync.

    Account-mode (paper vs live) is determined by the IB Gateway / TWS the
    client is connected to, not by code here. To switch, restart Gateway
    against the other login.
    """

    def __init__(
        self,
        ib_client,
        account: Optional[str] = None,
        fill_wait_seconds: float = 5.0,
        *,
        roll_days_before_expiry: int = DEFAULT_ROLL_DAYS_BEFORE_EXPIRY,
        today_fn: Optional[Callable[[], date]] = None,
    ) -> None:
        self._ib = ib_client
        self._account = account
        self._fill_wait = float(fill_wait_seconds)
        self._contract_cache: dict = {}
        self._roll_days = int(roll_days_before_expiry)
        self._today_fn = today_fn
        # Active brackets keyed by symbol. Each entry stores the parent +
        # child order IDs so poll_position can match fills back to their
        # leg and produce the right edge-triggered close state.
        self._active_brackets: dict[str, dict] = {}
        # Edge-triggered close events surfaced once per leg fire.
        self._pending_close: dict[str, PositionStatus] = {}
        # Track fill IDs we've already attributed to a bracket leg, so
        # repeated polls don't double-count the same fill.
        self._consumed_fill_ids: set = set()

    def submit_order(self, intent: FuturesOrderIntent) -> FuturesOrderAck:
        if intent.qty <= 0:
            return FuturesOrderAck(
                status="rejected",
                reason="non-positive quantity",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            )

        try:
            contract = resolve_front_month_future(
                self._ib,
                intent.symbol,
                self._contract_cache,
                roll_days_before_expiry=self._roll_days,
                today_fn=self._today_fn,
            )
        except Exception as exc:
            return FuturesOrderAck(
                status="rejected",
                reason=f"contract resolution failed: {exc}",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            )

        order = self._build_order(intent)
        if isinstance(order, FuturesOrderAck):  # _build_order returned a rejection
            return order

        if self._account:
            order.account = self._account
        if intent.client_order_id:
            order.orderRef = intent.client_order_id

        submission_time = _utc_now_iso()
        try:
            trade = self._ib.placeOrder(contract, order)
        except Exception as exc:
            return FuturesOrderAck(
                status="rejected",
                reason=f"placeOrder failed: {exc}",
                submission_time_utc=submission_time,
                order_id=intent.client_order_id,
            )

        # Wait briefly for fill ack. Paper-account fills are typically <1s.
        try:
            self._ib.waitOnUpdate(timeout=self._fill_wait)
        except Exception:
            pass  # best-effort wait; we still return whatever status we have

        return self._ack_from_trade(trade, submission_time)

    def cancel_order(self, order_id: str) -> FuturesOrderAck:
        try:
            open_trades = self._ib.openTrades()
        except Exception as exc:
            return FuturesOrderAck(
                status="rejected",
                reason=f"openTrades query failed: {exc}",
                submission_time_utc=_utc_now_iso(),
                order_id=order_id,
            )
        for trade in open_trades:
            if str(getattr(trade.order, "orderId", "")) == str(order_id):
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as exc:
                    return FuturesOrderAck(
                        status="rejected",
                        reason=f"cancelOrder failed: {exc}",
                        submission_time_utc=_utc_now_iso(),
                        order_id=order_id,
                    )
                return FuturesOrderAck(
                    status="cancelled",
                    order_id=order_id,
                    submission_time_utc=_utc_now_iso(),
                )
        return FuturesOrderAck(
            status="rejected",
            reason=f"order_id not found among open trades: {order_id}",
            submission_time_utc=_utc_now_iso(),
            order_id=order_id,
        )

    def get_open_positions(self) -> list[dict]:
        try:
            positions = self._ib.positions(self._account or "")
        except Exception:
            return []
        rows: list[dict] = []
        for p in positions:
            qty_signed = float(p.position)
            if qty_signed == 0:
                continue
            rows.append(
                {
                    "symbol": p.contract.symbol,
                    "side": "BUY" if qty_signed > 0 else "SELL",
                    "qty": abs(int(qty_signed)),
                    "entry_price": float(p.avgCost) if p.avgCost is not None else None,
                    "account": getattr(p, "account", None),
                }
            )
        return rows

    def reconcile(self) -> dict:
        try:
            open_trades_count = len(self._ib.openTrades())
        except Exception:
            open_trades_count = -1
        try:
            connected = bool(self._ib.isConnected())
        except Exception:
            connected = False
        return {
            "open_positions": len(self.get_open_positions()),
            "open_trades": open_trades_count,
            "connected": connected,
            "active_brackets": len(self._active_brackets),
        }

    # ----- bracket primitives ----------------------------------------------

    def submit_bracket(self, intent: BracketIntent) -> BracketAck:
        """Submit a MARKET parent + LIMIT TP child + STOP child as one OCO.

        All three orders share an OCA group so the broker cancels the
        unfilled siblings as soon as one fills. The parent transmits
        last (transmit=True only on the final leg) so all three reach
        the broker atomically — no risk of a parent reaching the book
        before its children.
        """
        symbol = intent.entry.symbol
        if symbol in self._active_brackets:
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason="bracket already active for symbol",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.entry.client_order_id,
                ),
                bracket_id=intent.bracket_id,
                reason="bracket already active for symbol",
            )

        if intent.entry.qty <= 0:
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason="non-positive quantity",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.entry.client_order_id,
                ),
                bracket_id=intent.bracket_id,
                reason="non-positive quantity",
            )
        if intent.entry.order_type.upper() != "MARKET":
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason="bracket parent must be MARKET in v1",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.entry.client_order_id,
                ),
                bracket_id=intent.bracket_id,
                reason="bracket parent must be MARKET in v1",
            )

        try:
            contract = resolve_front_month_future(
                self._ib,
                symbol,
                self._contract_cache,
                roll_days_before_expiry=self._roll_days,
                today_fn=self._today_fn,
            )
        except Exception as exc:
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason=f"contract resolution failed: {exc}",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.entry.client_order_id,
                ),
                bracket_id=intent.bracket_id,
                reason=str(exc),
            )

        if MarketOrder is None or LimitOrder is None or StopOrder is None:
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason="ib_insync not installed",
                    submission_time_utc=_utc_now_iso(),
                ),
                bracket_id=intent.bracket_id,
                reason="ib_insync not installed",
            )

        bracket_id = intent.bracket_id or f"bracket-{uuid.uuid4().hex[:12]}"
        oca_group = bracket_id  # one OCA group per bracket
        side = intent.entry.side.upper()
        reverse = "SELL" if side == "BUY" else "BUY"
        qty = int(intent.entry.qty)

        # Pre-allocate the parent's orderId so we can stamp it onto the
        # children's `parentId` BEFORE any placeOrder call. If parent gets
        # placed first and the children go in without parentId set, IBKR
        # treats them as independent orders — no parent-child enforcement.
        # The OCA group still binds them as one-cancels-other, but we lose
        # the semantic that children only become live after the parent fills.
        try:
            parent_order_id = int(self._ib.client.getReqId())
        except Exception as exc:
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason=f"could not allocate orderId: {exc}",
                    submission_time_utc=_utc_now_iso(),
                ),
                bracket_id=bracket_id,
                reason=f"could not allocate parent orderId: {exc}",
            )

        parent = MarketOrder(side, qty)
        parent.orderId = parent_order_id
        parent.tif = intent.entry.tif
        parent.transmit = False
        parent.orderRef = bracket_id
        if self._account:
            parent.account = self._account

        tp = LimitOrder(reverse, qty, float(intent.take_profit_price))
        tp.parentId = parent_order_id          # set BEFORE placeOrder
        tp.tif = "GTC"
        tp.ocaGroup = oca_group
        tp.ocaType = 1                       # CANCEL_WITH_BLOCK
        tp.transmit = False
        tp.orderRef = f"{bracket_id}-tp"
        if self._account:
            tp.account = self._account

        sl = StopOrder(reverse, qty, float(intent.stop_loss_price))
        sl.parentId = parent_order_id         # set BEFORE placeOrder
        sl.tif = "GTC"
        sl.ocaGroup = oca_group
        sl.ocaType = 1
        sl.transmit = True                   # last leg transmits all
        sl.orderRef = f"{bracket_id}-sl"
        if self._account:
            sl.account = self._account

        # Now submit all three. Children carry parentId from construction,
        # so the broker links them correctly on receipt.
        submission_time = _utc_now_iso()
        try:
            parent_trade = self._ib.placeOrder(contract, parent)
            tp_trade = self._ib.placeOrder(contract, tp)
            sl_trade = self._ib.placeOrder(contract, sl)
        except Exception as exc:
            return BracketAck(
                status="rejected",
                entry_ack=FuturesOrderAck(
                    status="rejected",
                    reason=f"placeOrder failed: {exc}",
                    submission_time_utc=submission_time,
                ),
                bracket_id=bracket_id,
                reason=str(exc),
            )

        # Wait briefly for parent fill (paper TWS typically <1s).
        try:
            self._ib.waitOnUpdate(timeout=self._fill_wait)
        except Exception:
            pass

        entry_ack = self._ack_from_trade(parent_trade, submission_time)
        tp_order_id = str(getattr(tp_trade.order, "orderId", "")) or ""
        sl_order_id = str(getattr(sl_trade.order, "orderId", "")) or ""

        # Record the bracket regardless of immediate fill state — we want
        # poll_position to know about it for any close detection below.
        self._active_brackets[symbol] = {
            "bracket_id": bracket_id,
            "parent_order_id": entry_ack.order_id or "",
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
            "side": side,
            "qty": qty,
            "tp_price": float(intent.take_profit_price),
            "sl_price": float(intent.stop_loss_price),
            "submission_time_utc": submission_time,
        }

        if entry_ack.status == "rejected":
            # Parent rejection invalidates the bracket; clean up.
            self._active_brackets.pop(symbol, None)
            try:
                self._ib.cancelOrder(tp_trade.order)
                self._ib.cancelOrder(sl_trade.order)
            except Exception:
                pass
            return BracketAck(
                status="rejected",
                entry_ack=entry_ack,
                take_profit_order_id=tp_order_id,
                stop_loss_order_id=sl_order_id,
                bracket_id=bracket_id,
                reason=entry_ack.reason,
            )

        return BracketAck(
            status="active",
            entry_ack=entry_ack,
            take_profit_order_id=tp_order_id,
            stop_loss_order_id=sl_order_id,
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

        # Query live position state at the broker.
        # Treat query failure as "unknown" — NOT "flat". The runner will
        # preserve its local state on unknown; treating a transient query
        # failure as flat would cause us to drop tracking of a real
        # open position.
        try:
            positions = self._ib.positions(self._account or "")
        except Exception as exc:
            return PositionStatus(
                state="unknown",
                note=f"positions query failed (broker disconnect or timeout): {exc}",
            )

        sym_positions = [
            p for p in positions
            if getattr(getattr(p, "contract", None), "symbol", "") == symbol
            and float(getattr(p, "position", 0)) != 0
        ]

        if sym_positions:
            p = sym_positions[0]
            qty_signed = float(p.position)
            return PositionStatus(
                state="open",
                side="BUY" if qty_signed > 0 else "SELL",
                qty=abs(int(qty_signed)),
                entry_price=float(p.avgCost) if p.avgCost is not None else None,
            )

        # Position is flat. Did one of our bracket legs just fill?
        bracket = self._active_brackets.get(symbol)
        if bracket is None:
            return PositionStatus(state="flat")

        # Inspect session fills to identify which child filled.
        try:
            fills = list(self._ib.fills() or [])
        except Exception:
            fills = []

        tp_id = bracket.get("tp_order_id", "")
        sl_id = bracket.get("sl_order_id", "")
        tp_fill = None
        sl_fill = None
        for f in fills:
            ex = getattr(f, "execution", None)
            if ex is None:
                continue
            ex_id = str(getattr(ex, "execId", "") or id(ex))
            if ex_id in self._consumed_fill_ids:
                continue
            ord_id = str(getattr(ex, "orderId", ""))
            if tp_id and ord_id == tp_id:
                tp_fill = (f, ex_id)
            elif sl_id and ord_id == sl_id:
                sl_fill = (f, ex_id)

        # Bracket is now consumed regardless of outcome.
        self._active_brackets.pop(symbol, None)

        if tp_fill is not None:
            f, ex_id = tp_fill
            self._consumed_fill_ids.add(ex_id)
            return PositionStatus(
                state="closed_tp",
                last_fill_price=float(f.execution.price) if f.execution.price else None,
                note=f"bracket TP filled (orderId={tp_id})",
            )
        if sl_fill is not None:
            f, ex_id = sl_fill
            self._consumed_fill_ids.add(ex_id)
            return PositionStatus(
                state="closed_stop",
                last_fill_price=float(f.execution.price) if f.execution.price else None,
                note=f"bracket SL filled (orderId={sl_id})",
            )

        return PositionStatus(
            state="closed_other",
            note="position flat with active bracket but no matching fill found",
        )

    def flatten_position(
        self,
        symbol: str,
        *,
        reason: str = "",
    ) -> FuturesOrderAck:
        """Cancel ALL working orders for the symbol, then submit offsetting MARKET.

        Cancels every open trade whose contract.symbol matches, not just
        children we know about via `_active_brackets`. This matters after
        reconcile-on-start: state was recovered from `ib.positions()` but
        we don't know the bracket child IDs from a previous process run,
        so the tracked-only cancellation would leave stale TP/SL working
        at the broker after we offset out — a residual exposure.
        """
        # Drop any tracked bracket immediately so poll_position doesn't
        # try to attribute the offsetting fill to a TP or SL leg.
        self._active_brackets.pop(symbol, None)

        # Cancel every working order on this contract regardless of source.
        try:
            open_trades = self._ib.openTrades() or []
        except Exception:
            open_trades = []
        cancelled = 0
        for trade in open_trades:
            contract = getattr(trade, "contract", None)
            trade_symbol = getattr(contract, "symbol", "") if contract else ""
            if trade_symbol != symbol:
                continue
            order = getattr(trade, "order", None)
            if order is None:
                continue
            try:
                self._ib.cancelOrder(order)
                cancelled += 1
            except Exception:
                pass

        # Find the live position to figure out the offsetting side.
        try:
            positions = self._ib.positions(self._account or "")
        except Exception as exc:
            return FuturesOrderAck(
                status="rejected",
                reason=f"positions query failed: {exc}",
                submission_time_utc=_utc_now_iso(),
            )
        sym_positions = [
            p for p in positions
            if getattr(getattr(p, "contract", None), "symbol", "") == symbol
            and float(getattr(p, "position", 0)) != 0
        ]
        if not sym_positions:
            return FuturesOrderAck(
                status="rejected",
                reason="no open position to flatten",
                submission_time_utc=_utc_now_iso(),
            )
        p = sym_positions[0]
        qty_signed = float(p.position)
        close_side = "SELL" if qty_signed > 0 else "BUY"
        ack = self.submit_order(FuturesOrderIntent(
            symbol=symbol,
            side=close_side,
            qty=abs(int(qty_signed)),
            order_type="MARKET",
            intent="time_stop",
        ))
        if ack.status == "filled":
            note = reason or "flatten"
            if cancelled:
                note = f"{note} (cancelled {cancelled} working order(s))"
            self._pending_close[symbol] = PositionStatus(
                state="closed_other",
                last_fill_price=ack.fill_price,
                note=note,
            )
        return ack

    # ----- helpers ----------------------------------------------------------

    def _build_order(self, intent: FuturesOrderIntent):
        order_type = intent.order_type.upper()
        side = intent.side.upper()
        if MarketOrder is None:
            return FuturesOrderAck(
                status="rejected",
                reason="ib_insync not installed",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            )
        if order_type == "MARKET":
            order = MarketOrder(side, intent.qty)
        elif order_type == "LIMIT":
            if intent.limit_price is None:
                return FuturesOrderAck(
                    status="rejected",
                    reason="LIMIT requires limit_price",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.client_order_id,
                )
            order = LimitOrder(side, intent.qty, intent.limit_price)
        elif order_type == "STOP":
            if intent.stop_price is None:
                return FuturesOrderAck(
                    status="rejected",
                    reason="STOP requires stop_price",
                    submission_time_utc=_utc_now_iso(),
                    order_id=intent.client_order_id,
                )
            order = StopOrder(side, intent.qty, intent.stop_price)
        else:
            return FuturesOrderAck(
                status="rejected",
                reason=f"unsupported order_type for IBKR adapter: {order_type}",
                submission_time_utc=_utc_now_iso(),
                order_id=intent.client_order_id,
            )
        order.tif = intent.tif
        return order

    def _ack_from_trade(self, trade, submission_time: str) -> FuturesOrderAck:
        status = getattr(getattr(trade, "orderStatus", None), "status", "Unknown")
        order = getattr(trade, "order", None)
        order_id = str(getattr(order, "orderId", "")) if order else ""
        filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
        avg_fill = getattr(trade.orderStatus, "avgFillPrice", None)
        avg_fill = float(avg_fill) if avg_fill not in (None, 0, 0.0) else None

        if status == "Filled":
            return FuturesOrderAck(
                status="filled",
                order_id=order_id,
                filled_qty=int(filled),
                fill_price=avg_fill,
                submission_time_utc=submission_time,
                fill_time_utc=_utc_now_iso(),
            )
        if status in ("Submitted", "PreSubmitted", "PendingSubmit"):
            return FuturesOrderAck(
                status="submitted",
                order_id=order_id,
                filled_qty=int(filled),
                fill_price=avg_fill,
                submission_time_utc=submission_time,
            )
        if status == "Cancelled":
            return FuturesOrderAck(
                status="cancelled",
                order_id=order_id,
                submission_time_utc=submission_time,
            )
        return FuturesOrderAck(
            status="rejected",
            order_id=order_id,
            reason=f"IBKR order status: {status}",
            submission_time_utc=submission_time,
        )
