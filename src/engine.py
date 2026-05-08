from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import uuid
from typing import List, Tuple
from zoneinfo import ZoneInfo

from .data.provider_factory import create_market_data_provider
from .data.yahoo import YahooMarketDataProvider
from .engines import VwapPullbackSignalEngine
from .execution_factory import create_execution_adapter
from .journal import EventJournal
from .models import InstrumentSelection, PlanResult, RiskDecision, SignalDecision
from .order_manager import OrderManager
from .planner import Planner, format_plan_card
from .risk import RiskEngine
from .services import OptionInstrumentService
from pathlib import Path
from .state import DailyState, DailyStateStore, OrderStateStore
from .positions import build_ledger_from_events
from .exits import ExitManager
from .option_symbols import build_occ_symbol


class PlannerApp:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.provider, self.provider_name = create_market_data_provider(config)
        self.signal_engine = VwapPullbackSignalEngine(config["strategy"])
        self.instrument_service = OptionInstrumentService(config["options"])
        self.risk_engine = RiskEngine()
        self.planner = Planner(self.signal_engine, self.instrument_service, self.risk_engine)
        self.journal = EventJournal(config)
        self.execution_cfg = config.get("execution", {})
        self.execution_enabled = bool(self.execution_cfg.get("enabled", False))
        self.execution_auto_submit = bool(self.execution_cfg.get("auto_submit", False))
        self.order_manager = None
        if self.execution_enabled:
            adapter = create_execution_adapter(config, self.journal)
            self.order_manager = OrderManager(adapter, self.journal)
        self.state_store = DailyStateStore(
            config.get("logging", {}).get("event_log_path", config.get("logging", {}).get("journal_path", "events.jsonl")),
            strict=config.get("logging", {}).get("pnl_strict", True),
        )
        self._last_health = True
        self._last_run_id: str | None = None
        self._live_mode = config.get("mode", "paper").lower() == "live"
        live_grade = config.get("live_grade", {}).get("enabled", False)
        self._fallback_enabled = config.get("data_provider_fallback", True) and not self._live_mode and not live_grade
        if self._live_mode:
            self.config.setdefault("options", {})["require_quote_time"] = True

    def run(self, log_to_journal: bool = True) -> Tuple[List[PlanResult], List[str]]:
        plans: List[PlanResult] = []
        errors: List[str] = []
        run_id = str(uuid.uuid4())
        self._last_run_id = run_id
        if self.config.get("risk_controls", {}).get("reset_counters_on_start", False):
            self.risk_engine.reset_counters()
        self.config.setdefault("runtime", {})["data_health_ok"] = True
        daily_state = self.state_store.load_today()
        reset_daily_limits = bool(self.config.get("risk_controls", {}).get("reset_daily_limits_on_start", False))
        if reset_daily_limits or self.config.get("risk_controls", {}).get("reset_counters_on_start", False):
            daily_state = DailyState()
            if log_to_journal:
                self.journal.log_event(
                    "daily_limits_reset",
                    {
                        "run_id": run_id,
                        "reason": "reset_on_start",
                    },
                )
        self.config["runtime"]["daily_state"] = {
            "realized_pnl": daily_state.realized_pnl,
            "last_loss_time_utc": daily_state.last_loss_time_utc,
        }
        if self._live_mode and self.provider_name != "webull":
            message = f"Live mode requires Webull provider, found {self.provider_name}"
            errors.append(message)
            self.config["runtime"]["data_health_ok"] = False
            if log_to_journal:
                self.journal.log_event(
                    "provider_blocked",
                    {
                        "run_id": run_id,
                        "reason": message,
                        "provider": self.provider_name,
                    },
                )
            return plans, errors

        session_date_exchange = None
        try:
            session_date_exchange = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        except Exception:
            session_date_exchange = None

        if self.execution_enabled and self.order_manager:
            exit_manager = ExitManager(self.config, self.provider, self.order_manager, self.journal)
            try:
                exit_manager.evaluate_and_submit(
                    session_date_exchange=session_date_exchange,
                    run_id=run_id,
                )
            except Exception as exc:
                if log_to_journal:
                    self.journal.log_event(
                        "exit_error",
                        {
                            "run_id": run_id,
                            "error": str(exc),
                        },
                    )
                errors.append(f"Exit manager error: {exc}")

        for symbol in self.config["strategy"]["symbols"]:
            decision_id = str(uuid.uuid4())
            try:
                df = self._get_intraday_bars(symbol, run_id, decision_id)
            except Exception as exc:
                message = f"{symbol}: Failed to fetch intraday data - {exc}"
                errors.append(message)
                self.config["runtime"]["data_health_ok"] = False
                if log_to_journal:
                    self.journal.log_event(
                        "error",
                        {
                            "run_id": run_id,
                            "decision_id": decision_id,
                            "symbol": symbol,
                            "provider": self.provider_name,
                            "stage": "intraday_bars",
                            "error": str(exc),
                        },
                    )
                continue

            chain = None
            try:
                chain = self._get_options_chain(symbol, run_id, decision_id)
            except Exception as exc:
                message = f"{symbol}: Failed to fetch options chain - {exc}"
                errors.append(message)
                self.config["runtime"]["data_health_ok"] = False
                if log_to_journal:
                    self.journal.log_event(
                        "error",
                        {
                            "run_id": run_id,
                            "decision_id": decision_id,
                            "symbol": symbol,
                            "provider": self.provider_name,
                            "stage": "options_chain",
                            "error": str(exc),
                        },
                    )

            self.config["runtime"]["data_health_ok"] = self.provider.health_check()
            plan, signal, selection, risk_decision = self.planner.build_plan(
                symbol,
                df,
                chain,
                self.config,
            )
            plan.run_id = run_id
            plan.decision_id = decision_id
            plans.append(plan)
            if log_to_journal:
                self._log_decisions(plan, signal, selection, risk_decision, run_id, decision_id)
            self._submit_if_enabled(plan, run_id, decision_id)

        self._log_reconnect_if_needed(log_to_journal)
        if log_to_journal and self.execution_enabled:
            self._log_reconcile(run_id)
        return plans, errors

    def _get_intraday_bars(self, symbol: str, run_id: str, decision_id: str):
        try:
            return self.provider.get_intraday_bars(
                symbol,
                self.config["strategy"]["history_period"],
                self.config["strategy"]["interval"],
            )
        except Exception as exc:
            if self._attempt_fallback(run_id, decision_id, str(exc)):
                return self.provider.get_intraday_bars(
                    symbol,
                    self.config["strategy"]["history_period"],
                    self.config["strategy"]["interval"],
                )
            raise

    def _get_options_chain(self, symbol: str, run_id: str, decision_id: str):
        try:
            return self.provider.get_options_chain(symbol, self.config["options"]["target_dte"])
        except Exception as exc:
            if self._attempt_fallback(run_id, decision_id, str(exc)):
                return self.provider.get_options_chain(symbol, self.config["options"]["target_dte"])
            raise

    def _attempt_fallback(self, run_id: str, decision_id: str, error_text: str) -> bool:
        if not self._fallback_enabled:
            return False
        if self.provider_name.startswith("yahoo"):
            return False
        self.provider = YahooMarketDataProvider()
        self.provider_name = "yahoo-fallback"
        self.journal.log_event(
            "provider_fallback",
            {
                "run_id": run_id,
                "decision_id": decision_id,
                "error": error_text,
                "fallback": self.provider_name,
            },
        )
        return True

    def _log_decisions(
        self,
        plan: PlanResult,
        signal: SignalDecision,
        selection: InstrumentSelection,
        risk_decision: RiskDecision,
        run_id: str,
        decision_id: str,
    ) -> None:
        self.journal.log_event("signal", self._signal_payload(signal, run_id, decision_id))
        self.journal.log_event(
            "instrument_selection",
            {
                "run_id": run_id,
                "decision_id": decision_id,
                "symbol": plan.symbol,
                "decision_time_utc": plan.decision_time_utc,
                "selected_contract": selection.option_contract.to_dict()
                if selection.option_contract
                else None,
                "top_candidates": selection.top_candidates,
                "warnings": selection.warnings,
                "reject_reasons": selection.reject_reasons,
            },
        )
        plan_payload = plan.to_dict()
        plan_payload["run_id"] = run_id
        plan_payload["decision_id"] = decision_id
        self.journal.log_event("plan", plan_payload)

        if selection.reject_reasons or risk_decision.reject_reasons or plan.reject_reasons:
            self.journal.log_event(
                "reject_reason",
                {
                    "run_id": run_id,
                    "decision_id": decision_id,
                    "symbol": plan.symbol,
                    "decision_time_utc": plan.decision_time_utc,
                    "reasons": plan.reject_reasons,
                },
            )

        benchmark_cfg = self.config.get("benchmark", {})
        submit_rejected = bool(benchmark_cfg.get("enabled", False) and benchmark_cfg.get("submit_rejected", False))
        allow_intent = plan.status == "ALLOWED" or (plan.status == "BENCHMARK" and submit_rejected)
        if allow_intent:
            self.journal.log_event(
                "order_intent",
                {
                    "run_id": run_id,
                    "decision_id": decision_id,
                    "symbol": plan.symbol,
                    "provider": self.provider_name,
                    "direction": plan.direction,
                    "contracts": plan.contracts,
                    "decision_time_utc": plan.decision_time_utc,
                    "submission_time_utc": None,
                    "stop": plan.invalidation,
                    "targets": plan.targets,
                    "data_health_score": plan.data_health_score,
                    "risk_pct_base": plan.risk_pct_base,
                    "risk_pct_used": plan.risk_pct_used,
                    "atr_target_pct": plan.atr_target_pct,
                    "stop_mode": plan.stop_mode,
                    "atr_value": plan.atr_value,
                    "atr_pct": plan.atr_pct,
                    "higher_timeframe_trend": plan.higher_timeframe_trend,
                    "sentiment_value": plan.sentiment_value,
                    "sentiment_label": plan.sentiment_label,
                    "arrival_mid": plan.option_contract.mid if plan.option_contract else None,
                    "arrival_bid": plan.option_contract.bid if plan.option_contract else None,
                    "arrival_ask": plan.option_contract.ask if plan.option_contract else None,
                    "arrival_spread_pct": plan.option_contract.spread_pct if plan.option_contract else None,
                    "quote_time_utc": plan.option_contract.quote_time_utc if plan.option_contract else None,
                    "spread_pct": plan.option_contract.spread_pct if plan.option_contract else None,
                    "next_bar_mid": None,
                    "next_bar_time_utc": None,
                    "implied_volatility": plan.option_contract.implied_volatility if plan.option_contract else None,
                    "option": plan.option_contract.to_dict() if plan.option_contract else None,
                },
            )

    def _log_reconnect_if_needed(self, log_to_journal: bool) -> None:
        current_health = self.provider.health_check()
        if current_health and not self._last_health and log_to_journal:
            self.journal.log_event(
                "reconnect",
                {
                    "run_id": self._last_run_id,
                    "status": "data_provider_restored",
                },
            )
        self._last_health = current_health

    def _log_reconcile(self, run_id: str) -> None:
        event_log_path = Path(
            self.config.get("logging", {}).get("event_log_path", "events.jsonl")
        )
        session_date_exchange = None
        try:
            session_date_exchange = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        except Exception:
            session_date_exchange = None
        open_orders = OrderStateStore(str(event_log_path)).load_open_orders(session_date_exchange)
        contract_multiplier = (
            self.execution_cfg.get("paper", {}).get("contract_multiplier", 100) or 100
        )
        ledger = build_ledger_from_events(
            event_log_path,
            session_date_exchange=session_date_exchange,
            contract_multiplier=contract_multiplier,
        )
        self.journal.log_event(
            "reconcile",
            {
                "run_id": run_id,
                "open_orders": len(open_orders),
                "open_positions": len(ledger.positions),
            },
        )

    def _signal_payload(self, signal: SignalDecision, run_id: str, decision_id: str) -> dict:
        payload = asdict(signal)
        payload["run_id"] = run_id
        payload["decision_id"] = decision_id
        if signal.bar_timestamp.tzinfo is None:
            payload["bar_timestamp"] = signal.bar_timestamp.replace(tzinfo=timezone.utc).isoformat()
        else:
            payload["bar_timestamp"] = signal.bar_timestamp.astimezone(timezone.utc).isoformat()
        return payload

    def _submit_if_enabled(self, plan: PlanResult, run_id: str, decision_id: str) -> None:
        if not self.execution_enabled or not self.order_manager:
            return
        if not self.execution_auto_submit:
            plan.execution_status = "ready"
            return
        benchmark_cfg = self.config.get("benchmark", {})
        submit_rejected = bool(benchmark_cfg.get("enabled", False) and benchmark_cfg.get("submit_rejected", False))
        if plan.status != "ALLOWED" and not (plan.status == "BENCHMARK" and submit_rejected):
            return
        if plan.option_contract is None or plan.contracts <= 0:
            plan.execution_status = "skipped"
            plan.execution_message = "Missing contract or size"
            return
        try:
            payload = self._build_order_payload(plan, run_id, decision_id)
            result = self.order_manager.submit_bracket_intent(payload, log_intent=False)
            plan.execution_status = result.get("status")
            plan.execution_message = result.get("reason")
            plan.order_id = result.get("order_id")
        except Exception as exc:
            self.journal.log_event(
                "execution_error",
                {
                    "run_id": run_id,
                    "decision_id": decision_id,
                    "symbol": plan.symbol,
                    "error": str(exc),
                },
            )
            plan.execution_status = "error"
            plan.execution_message = str(exc)

    def _build_order_payload(self, plan: PlanResult, run_id: str, decision_id: str) -> dict:
        option = plan.option_contract
        exec_cfg = self.execution_cfg
        order_type = (exec_cfg.get("order_type") or "LIMIT").upper()
        limit_price = option.mid if option else None
        if order_type == "MARKET":
            limit_price = None
        option_symbol = option.option_symbol if option else None
        if isinstance(option_symbol, str):
            option_symbol = option_symbol.strip()
        if option_symbol == plan.symbol:
            option_symbol = None
        if not option_symbol and option:
            option_symbol = build_occ_symbol(
                plan.symbol,
                option.expiration,
                option.option_type or plan.direction,
                option.strike,
            )
        return {
            "run_id": run_id,
            "decision_id": decision_id,
            "symbol": plan.symbol,
            "asset_class": "OPTION",
            "side": "BUY",
            "direction": plan.direction,
            "contracts": plan.contracts,
            "qty": plan.contracts,
            "order_type": order_type,
            "limit_price": limit_price,
            "tif": exec_cfg.get("tif", "DAY"),
            "invalidation": plan.invalidation,
            "targets": plan.targets,
            "decision_time_utc": plan.decision_time_utc,
            "option_symbol": option_symbol,
            "expiration": option.expiration if option else None,
            "strike": option.strike if option else None,
            "option_type": option.option_type if option else None,
            "bid": option.bid if option else None,
            "ask": option.ask if option else None,
            "mid": option.mid if option else None,
            "last_price": option.last_price if option else None,
            "underlying_price": option.underlying_price if option else None,
            "quote_time_utc": option.quote_time_utc if option else None,
        }


def generate_plans(config: dict, log_to_journal: bool = True) -> Tuple[List[PlanResult], List[str]]:
    app = PlannerApp(config)
    return app.run(log_to_journal=log_to_journal)


def print_plans(plans: List[PlanResult], config: dict) -> None:
    output_mode = config.get("output", {}).get("mode", "detailed")
    for plan in plans:
        print(format_plan_card(plan, output_mode))
        print("")
