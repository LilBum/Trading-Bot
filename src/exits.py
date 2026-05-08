from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .execution_adapter import PaperExecutionAdapter
from .journal import EventJournal
from .order_manager import OrderManager
from .positions import build_ledger_from_events, parse_iso_time
from .position_state import PositionStateStore
from .state import OrderStateStore
from .option_symbols import build_occ_symbol


@dataclass
class ExitDecision:
    position_key: str
    symbol: str
    expiration: str
    strike: float
    option_type: str
    qty: int
    entry_price: float
    current_mid: float
    current_bid: float | None = None
    current_ask: float | None = None
    reason: str = ""


class ExitManager:
    def __init__(
        self,
        config: dict,
        provider,
        order_manager: Optional[OrderManager],
        journal: EventJournal,
    ) -> None:
        self.config = config
        self.provider = provider
        self.order_manager = order_manager
        self.journal = journal
        self.exit_cfg = config.get("exits", {})
        self.exec_cfg = config.get("execution", {})
        self._paper_exit_adapter: Optional[PaperExecutionAdapter] = None

    def evaluate_and_submit(
        self,
        session_date_exchange: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[ExitDecision]:
        decisions: list[ExitDecision] = []
        event_log_path = Path(
            self.config.get("logging", {}).get("event_log_path", "events.jsonl")
        )
        contract_multiplier = (
            self.exec_cfg.get("paper", {}).get("contract_multiplier", 100) or 100
        )
        ledger = build_ledger_from_events(
            event_log_path,
            session_date_exchange=session_date_exchange,
            contract_multiplier=contract_multiplier,
        )
        if not ledger.positions:
            return decisions

        state_path = Path(
            self.config.get("logging", {}).get("positions_state_path", "positions_state.json")
        )
        state_store = PositionStateStore(state_path)
        state_store.load()

        broker_positions = self._load_broker_positions()

        open_orders = OrderStateStore(str(event_log_path)).load_open_orders(session_date_exchange)
        open_order_keys = set()
        for payload in open_orders.values():
            order = payload.get("order_payload") or {}
            symbol = order.get("symbol")
            expiration = order.get("expiration")
            strike = order.get("strike")
            option_type = order.get("option_type") or order.get("direction")
            side = (order.get("side") or "").upper()
            if symbol and expiration and strike is not None and option_type and side == "SELL":
                key = f"{symbol}|{expiration}|{float(strike):.2f}|{option_type}"
                open_order_keys.add(key)

        for key, position in ledger.positions.items():
            if position.qty <= 0:
                continue
            if key in open_order_keys:
                continue
            exit_qty = position.qty
            if broker_positions:
                option_symbol = build_occ_symbol(
                    position.symbol,
                    position.expiration,
                    position.option_type,
                    position.strike,
                )
                if option_symbol:
                    broker_qty = broker_positions.get(option_symbol)
                    if broker_qty is not None:
                        exit_qty = min(exit_qty, int(broker_qty))
                        if exit_qty <= 0:
                            continue
            current_quote = self._get_current_quote(
                position.symbol,
                position.expiration,
                position.strike,
                position.option_type,
            )
            if current_quote is None:
                continue
            activation_pct = self.exit_cfg.get("trailing_stop_activation_pct")
            current_mid = current_quote["mid"]
            state = state_store.update(key, current_mid, position.avg_price, activation_pct)
            decision = self._exit_decision(position, current_mid, exit_qty)
            trailing_pct = self._effective_trailing_pct(position.symbol)
            if decision is None and trailing_pct is not None and state.trailing_active:
                if current_mid <= state.peak_mid * (1.0 - float(trailing_pct)):
                    decision = ExitDecision(
                        position_key=position.key(),
                        symbol=position.symbol,
                        expiration=position.expiration,
                        strike=position.strike,
                        option_type=position.option_type,
                        qty=exit_qty,
                        entry_price=position.avg_price,
                        current_mid=current_mid,
                        current_bid=current_quote.get("bid"),
                        current_ask=current_quote.get("ask"),
                        reason="trailing_stop",
                    )
            if decision is None:
                continue
            decisions.append(decision)
            self._log_exit_signal(decision, run_id)
            if self._should_submit():
                self._submit_exit(decision, position, run_id)

        state_store.prune(set(ledger.positions.keys()))
        state_store.save()
        return decisions

    def _should_submit(self) -> bool:
        if not self.exec_cfg.get("enabled", False):
            return False
        return bool(self.exec_cfg.get("exit_auto_submit", self.exec_cfg.get("auto_submit", False)))

    def _exit_decision(self, position, current_mid: float, exit_qty: int) -> Optional[ExitDecision]:
        take_profit = self.exit_cfg.get("take_profit_pct")
        stop_loss = self.exit_cfg.get("stop_loss_pct")
        max_hold_minutes = self.exit_cfg.get("max_hold_minutes")
        exit_before_close = self.exit_cfg.get("exit_before_close_minutes")

        entry_price = position.avg_price
        reason = None
        if take_profit is not None and entry_price > 0:
            if current_mid >= entry_price * (1.0 + float(take_profit)):
                reason = "take_profit"
        if reason is None and stop_loss is not None and entry_price > 0:
            if current_mid <= entry_price * (1.0 - float(stop_loss)):
                reason = "stop_loss"
        if reason is None and max_hold_minutes is not None:
            entry_time = parse_iso_time(position.entry_time_utc)
            if entry_time:
                age_minutes = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60.0
                if age_minutes >= float(max_hold_minutes):
                    reason = "max_hold_time"
        if reason is None and exit_before_close is not None:
            if self._near_close(float(exit_before_close)):
                reason = "end_of_day"
        if reason is None:
            return None

        return ExitDecision(
            position_key=position.key(),
            symbol=position.symbol,
            expiration=position.expiration,
            strike=position.strike,
            option_type=position.option_type,
            qty=exit_qty,
            entry_price=entry_price,
            current_mid=current_mid,
            reason=reason,
        )

    def _get_current_quote(
        self,
        symbol: str,
        expiration: str,
        strike: float,
        option_type: str,
    ) -> Optional[dict]:
        target_dte = self.config.get("options", {}).get("target_dte", 1)
        dte_from_expiration = self._dte_from_expiration(expiration)
        if dte_from_expiration is not None:
            target_dte = dte_from_expiration
        try:
            chain_data = self.provider.get_options_chain(symbol, target_dte)
        except Exception:
            return None
        if chain_data is None:
            return None
        _, chain = chain_data
        subset = chain[
            (chain["expiration"] == expiration)
            & (chain["strike"] == strike)
            & (chain["option_type"] == option_type)
        ]
        if subset.empty:
            return None
        row = subset.iloc[0]
        bid = row.get("bid")
        ask = row.get("ask")
        last_price = row.get("last_price")
        mid = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (float(bid) + float(ask)) / 2.0
        elif last_price is not None and last_price > 0:
            mid = float(last_price)
        if mid is None:
            return None
        return {"mid": mid, "bid": bid, "ask": ask}

    def _log_exit_signal(self, decision: ExitDecision, run_id: Optional[str]) -> None:
        self.journal.log_event(
            "exit_signal",
            {
                "run_id": run_id,
                "symbol": decision.symbol,
                "expiration": decision.expiration,
                "strike": decision.strike,
                "option_type": decision.option_type,
                "qty": decision.qty,
                "entry_price": decision.entry_price,
                "current_mid": decision.current_mid,
                "reason": decision.reason,
            },
        )

    def _submit_exit(self, decision: ExitDecision, position, run_id: Optional[str]) -> None:
        if not self.order_manager:
            return
        order_type = (self.exec_cfg.get("exit_order_type") or self.exec_cfg.get("order_type") or "LIMIT").upper()
        exit_limit_mode = self.exit_cfg.get("exit_limit_mode", "mid")
        limit_price = None
        if order_type != "MARKET":
            if exit_limit_mode == "marketable":
                if decision.current_bid is not None:
                    limit_price = float(decision.current_bid)
                else:
                    limit_price = decision.current_mid
            else:
                limit_price = decision.current_mid
        payload = {
            "run_id": run_id,
            "symbol": decision.symbol,
            "asset_class": "OPTION",
            "side": "SELL",
            "direction": decision.option_type,
            "contracts": int(decision.qty),
            "qty": int(decision.qty),
            "order_type": order_type,
            "limit_price": limit_price,
            "tif": self.exec_cfg.get("tif", "DAY"),
            "expiration": decision.expiration,
            "strike": float(decision.strike),
            "option_type": decision.option_type,
            "entry_price": decision.entry_price,
            "entry_time_utc": position.entry_time_utc,
            "exit_reason": decision.reason,
            "bid": decision.current_bid,
            "ask": decision.current_ask,
            "mid": decision.current_mid,
        }
        option_symbol = build_occ_symbol(
            decision.symbol,
            decision.expiration,
            decision.option_type,
            decision.strike,
        )
        if option_symbol:
            payload["option_symbol"] = option_symbol
        if self._should_use_paper_exit():
            self.journal.log_event(
                "execution_warning",
                {
                    "reason": "Paper-mode exit simulated via paper adapter.",
                    "adapter": self.exec_cfg.get("adapter"),
                    "mode": self.exec_cfg.get("mode"),
                },
            )
            self.journal.log_event("order_intent", payload)
            self._paper_exit().submit_order(payload)
            return
        result = self.order_manager.submit_bracket_intent(payload)
        if self._should_fallback_exit(result):
            self.journal.log_event(
                "execution_warning",
                {
                    "reason": "Exit rejected by adapter; simulating via paper adapter.",
                    "adapter": self.exec_cfg.get("adapter"),
                    "mode": self.exec_cfg.get("mode"),
                    "response": result,
                },
            )
            self._paper_exit().submit_order(payload)

    def _should_use_paper_exit(self) -> bool:
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        if mode == "live":
            return False
        explicit = self.exec_cfg.get("exit_use_paper")
        if explicit is not None:
            return bool(explicit)
        adapter = (self.exec_cfg.get("adapter") or "null").lower()
        if adapter == "paper":
            return False
        if adapter == "tradier":
            return bool(self.exec_cfg.get("tradier", {}).get("simulate_fill_on_ack", False))
        if adapter == "webull":
            webull_cfg = self.exec_cfg.get("webull", {})
            return bool(webull_cfg.get("paper_only", False))
        return False

    def _should_fallback_exit(self, result: dict | None) -> bool:
        if not result:
            return False
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        if mode == "live":
            return False
        adapter = (self.exec_cfg.get("adapter") or "null").lower()
        if adapter != "tradier":
            return False
        if not self.exec_cfg.get("tradier", {}).get("simulate_fill_on_ack", False):
            return False
        status = (result.get("status") or "").lower()
        return status == "rejected"

    def _paper_exit(self) -> PaperExecutionAdapter:
        if self._paper_exit_adapter is None:
            self._paper_exit_adapter = PaperExecutionAdapter(self.config, self.journal)
        return self._paper_exit_adapter

    def _near_close(self, minutes: float) -> bool:
        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        if now.weekday() >= 5:
            return False
        close_time = datetime.combine(now.date(), time(16, 0), tzinfo=eastern)
        delta = (close_time - now).total_seconds() / 60.0
        return 0 <= delta <= minutes

    def _dte_from_expiration(self, expiration: str) -> Optional[int]:
        try:
            exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        except ValueError:
            return None
        today = datetime.now(ZoneInfo("America/New_York")).date()
        return max(0, (exp_date - today).days)

    def _effective_trailing_pct(self, symbol: str) -> Optional[float]:
        trailing_pct = self.exit_cfg.get("trailing_stop_pct")
        if trailing_pct is None:
            return None
        if not self.exit_cfg.get("trailing_use_chop", False):
            return trailing_pct
        chop_pct = self.exit_cfg.get("trailing_stop_pct_chop")
        if chop_pct is None:
            return trailing_pct
        try:
            df = self.provider.get_intraday_bars(
                symbol,
                self.config.get("strategy", {}).get("history_period", "1d"),
                self.config.get("strategy", {}).get("interval", "5m"),
            )
            from .indicators import compute_vwap
            from .regime import count_vwap_crosses

            vwap = compute_vwap(df)
            crosses = count_vwap_crosses(
                df["Close"], vwap, self.config.get("strategy", {}).get("chop_lookback_minutes", 30)
            )
            max_crosses = self.config.get("strategy", {}).get("max_vwap_crosses", 4)
            if crosses >= max_crosses:
                return chop_pct
        except Exception:
            return trailing_pct
        return trailing_pct

    def _load_broker_positions(self) -> dict[str, int]:
        if not self.exec_cfg.get("exit_sync_broker_positions", True):
            return {}
        if not self.order_manager:
            return {}
        adapter = getattr(self.order_manager, "execution_adapter", None)
        if adapter is None:
            return {}
        get_positions = getattr(adapter, "get_positions", None)
        if not callable(get_positions):
            return {}
        try:
            positions = get_positions()
            if isinstance(positions, dict):
                return positions
        except Exception:
            return {}
        return {}
