from __future__ import annotations

from typing import Optional, Tuple
from datetime import datetime, timezone

import pandas as pd

from .engines import VwapPullbackSignalEngine
from .models import InstrumentSelection, PlanResult, RiskDecision, SignalDecision
from .risk import RiskEngine
from .services import OptionInstrumentService
from .orb import compute_orb_info


class Planner:
    def __init__(
        self,
        signal_engine: VwapPullbackSignalEngine,
        instrument_service: OptionInstrumentService,
        risk_engine: RiskEngine,
    ) -> None:
        self.signal_engine = signal_engine
        self.instrument_service = instrument_service
        self.risk_engine = risk_engine

    def build_plan(
        self,
        symbol: str,
        df_1m: pd.DataFrame,
        chain_data: Optional[tuple[str, pd.DataFrame]],
        config: dict,
    ) -> Tuple[PlanResult, SignalDecision, InstrumentSelection, RiskDecision]:
        signal = self.signal_engine.evaluate(symbol, df_1m, config)
        orb_info = compute_orb_info(symbol, df_1m, config)
        initial_rejects = list(signal.reject_reasons)

        orb_cfg = config.get("orb", {})
        orb_primary = bool(orb_cfg.get("primary", False))
        orb_fallback = bool(orb_cfg.get("fallback_to_vwap", False))

        def _keep_orb_rejects(reasons: list[str]) -> list[str]:
            keep: list[str] = []
            for reason in reasons:
                if reason.startswith("Stale market data"):
                    keep.append(reason)
                elif reason.startswith("Sentiment"):
                    keep.append(reason)
            return keep

        if orb_primary:
            if orb_info and orb_info.get("status") == "confirmed":
                signal.direction = orb_info.get("direction", "NONE")
                signal.setup = "ORB"
                signal.entry_trigger = "ORB confirmed"
                signal.reject_reasons = _keep_orb_rejects(initial_rejects)
            elif not orb_fallback:
                status = orb_info.get("status") if orb_info else "unavailable"
                signal.direction = "NONE"
                signal.setup = "ORB"
                signal.entry_trigger = f"ORB status: {status}"
                signal.reject_reasons = [f"ORB not confirmed ({status})"]
                signal.reject_reasons.extend(_keep_orb_rejects(initial_rejects))

        benchmark_cfg = config.get("benchmark", {})
        if (
            benchmark_cfg.get("use_orb_signal", False)
            and signal.direction == "NONE"
            and orb_info
            and orb_info.get("status") == "confirmed"
        ):
            signal.direction = orb_info.get("direction", "NONE")
            signal.setup = "ORB"
            signal.entry_trigger = "ORB confirmed"
            signal.reject_reasons = [
                reason for reason in signal.reject_reasons if reason != "No valid setup conditions"
            ]

        reject_reasons = list(signal.reject_reasons)
        warnings = list(signal.warnings)
        option_contract = None
        selection = InstrumentSelection(None)

        if signal.direction != "NONE":
            selection = self.instrument_service.select_contract(
                symbol,
                chain_data,
                signal.direction,
                df_1m["Close"].iloc[-1],
                signal.decision_time_utc,
            )
            option_contract = selection.option_contract
            reject_reasons.extend(selection.reject_reasons)
            warnings.extend(selection.warnings)

        risk_decision = self._evaluate_risk(
            symbol,
            option_contract,
            signal,
            config,
        )
        reject_reasons.extend(risk_decision.reject_reasons)
        warnings.extend(risk_decision.warnings)

        data_health_score = self._compute_data_health_score(signal, option_contract, config)
        min_score = config.get("data_quality", {}).get("min_score")
        if min_score is not None and data_health_score < min_score:
            reject_reasons.append("Data health below minimum")

        if config.get("risk_controls", {}).get("require_bracket", False):
            if not signal.invalidation or not signal.targets:
                reject_reasons.append("Bracket exit required")

        status = "ALLOWED" if not reject_reasons else "REJECTED"

        underlying_price = float(df_1m["Close"].iloc[-1])

        benchmark_enabled = bool(benchmark_cfg.get("enabled", False))
        submit_rejected = bool(benchmark_cfg.get("submit_rejected", False))
        allow_override = benchmark_enabled and submit_rejected and config.get("mode", "paper").lower() != "live"
        override_contracts = int(benchmark_cfg.get("override_contracts", 0) or 0)
        if status == "REJECTED" and allow_override and signal.direction != "NONE" and option_contract is not None:
            status = "BENCHMARK"
            warnings.append("Benchmark override: rejected reasons ignored for paper submit.")
            if risk_decision.contracts <= 0 and override_contracts > 0:
                estimated_premium = option_contract.mid * 100.0 * override_contracts
                estimated_risk = estimated_premium * config.get("position_sizing", {}).get("premium_stop_pct", 0.25)
                risk_decision = RiskDecision(
                    allowed=True,
                    contracts=override_contracts,
                    estimated_risk=estimated_risk,
                    estimated_premium=estimated_premium,
                    risk_pct_base=risk_decision.risk_pct_base,
                    risk_pct_used=risk_decision.risk_pct_used,
                    atr_target_pct=risk_decision.atr_target_pct,
                    stop_mode=risk_decision.stop_mode,
                    risk_per_contract=risk_decision.risk_per_contract,
                    reject_reasons=risk_decision.reject_reasons,
                    warnings=risk_decision.warnings,
                )
        plan = PlanResult(
            symbol=symbol,
            timestamp=signal.bar_timestamp,
            setup=signal.setup,
            direction=signal.direction,
            entry_trigger=signal.entry_trigger,
            invalidation=signal.invalidation,
            premium_stop=signal.premium_stop,
            targets=signal.targets,
            contracts=risk_decision.contracts,
            estimated_risk=round(risk_decision.estimated_risk, 2),
            estimated_premium=round(risk_decision.estimated_premium, 2),
            option_contract=option_contract,
            status=status,
            reject_reasons=reject_reasons,
            warnings=warnings,
            regime_info=signal.regime_info,
            decision_time_utc=signal.decision_time_utc,
            data_health_score=round(data_health_score, 3),
            underlying_price=underlying_price,
            atr_value=signal.atr_value,
            atr_pct=signal.atr_pct,
            higher_timeframe_trend=signal.higher_timeframe_trend,
            sentiment_value=signal.sentiment_value,
            sentiment_label=signal.sentiment_label,
            risk_pct_base=risk_decision.risk_pct_base,
            risk_pct_used=risk_decision.risk_pct_used,
            atr_target_pct=risk_decision.atr_target_pct,
            stop_mode=risk_decision.stop_mode,
            orb=orb_info,
        )
        return plan, signal, selection, risk_decision

    def build(
        self,
        symbol: str,
        df_1m: pd.DataFrame,
        chain_data: Optional[tuple[str, pd.DataFrame]],
        config: dict,
    ) -> PlanResult:
        plan, _, _, _ = self.build_plan(symbol, df_1m, chain_data, config)
        return plan

    def _evaluate_risk(
        self,
        symbol: str,
        option_contract,
        signal,
        config: dict,
    ) -> RiskDecision:
        if signal.direction == "NONE":
            return RiskDecision(
                allowed=False,
                contracts=0,
                estimated_risk=0.0,
                estimated_premium=0.0,
                reject_reasons=[],
                warnings=[],
            )
        if option_contract is None:
            return RiskDecision(
                allowed=False,
                contracts=0,
                estimated_risk=0.0,
                estimated_premium=0.0,
                reject_reasons=[],
                warnings=[],
            )
        return self.risk_engine.assess(
            symbol,
            option_contract,
            signal.decision_time_utc,
            signal.direction,
            config,
            atr_value=signal.atr_value,
            atr_pct=signal.atr_pct,
        )

    def _compute_data_health_score(
        self,
        signal: SignalDecision,
        option_contract,
        config: dict,
    ) -> float:
        score = 1.0
        data_quality = config.get("data_quality", {})
        if not config.get("runtime", {}).get("data_health_ok", True):
            return 0.0

        stale_penalty = data_quality.get("stale_penalty", 0.5)
        bar_age_penalty = data_quality.get("bar_age_penalty", 0.2)
        quote_penalty = data_quality.get("quote_penalty", 0.3)
        iv_penalty = data_quality.get("missing_iv_penalty", 0.2)

        if any("Stale market data" in reason for reason in signal.reject_reasons):
            score -= stale_penalty
        max_bar_age = data_quality.get("max_bar_age_minutes")
        if max_bar_age is not None:
            bar_ts = signal.bar_timestamp
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            bar_age = (datetime.now(timezone.utc) - bar_ts.astimezone(timezone.utc)).total_seconds() / 60.0
            if bar_age > max_bar_age:
                score -= bar_age_penalty

        if option_contract is not None:
            max_quote_age_seconds = config.get("options", {}).get("max_quote_age_seconds")
            if max_quote_age_seconds is not None:
                age_seconds = self._quote_age_seconds(option_contract.quote_time_utc)
                if age_seconds is None or age_seconds > max_quote_age_seconds:
                    score -= quote_penalty
            else:
                max_quote_age = config.get("options", {}).get("max_quote_age_minutes")
                if max_quote_age is not None:
                    age_minutes = self._quote_age_minutes(option_contract.quote_time_utc)
                    if age_minutes is None or age_minutes > max_quote_age:
                        score -= quote_penalty

            if option_contract.implied_volatility <= 0:
                if config.get("options", {}).get("require_iv_for_short_dte", False):
                    score -= iv_penalty

        return max(score, 0.0)

    def _quote_age_minutes(self, quote_time_utc: str) -> Optional[float]:
        try:
            quote_dt = datetime.fromisoformat(quote_time_utc).astimezone(timezone.utc)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - quote_dt).total_seconds() / 60.0

    def _quote_age_seconds(self, quote_time_utc: str) -> Optional[float]:
        try:
            quote_dt = datetime.fromisoformat(quote_time_utc).astimezone(timezone.utc)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - quote_dt).total_seconds()


def build_trade_plan(
    symbol: str,
    df_1m: pd.DataFrame,
    chain_data: Optional[tuple[str, pd.DataFrame]],
    config: dict,
) -> PlanResult:
    planner = Planner(
        VwapPullbackSignalEngine(config["strategy"]),
        OptionInstrumentService(config["options"]),
        RiskEngine(),
    )
    return planner.build(symbol, df_1m, chain_data, config)


def _format_plan_card_simple(plan: PlanResult) -> str:
    header = f"TRADE PLAN - {plan.symbol} - {plan.status}"
    lines = ["=" * len(header), header, "=" * len(header)]
    lines.append(f"Time: {plan.timestamp:%Y-%m-%d %H:%M:%S %Z}")
    if plan.status in {"ALLOWED", "BENCHMARK"}:
        direction = "Buy CALL" if plan.direction == "CALL" else "Buy PUT"
    else:
        direction = "No trade"
    lines.append(f"Action: {direction}")
    if plan.option_contract:
        option = plan.option_contract
        lines.append(f"Strike: {option.strike:.2f}")
        lines.append(f"Expiration: {option.expiration}")
    else:
        lines.append("Strike: N/A")
        lines.append("Expiration: N/A")
    if plan.execution_status:
        lines.append(f"Execution: {plan.execution_status}")
    lines.append("=" * len(header))
    return "\n".join(lines)


def format_plan_card(plan: PlanResult, mode: str = "detailed") -> str:
    if mode == "simple":
        return _format_plan_card_simple(plan)
    header = f"TRADE PLAN CARD - {plan.symbol} - {plan.direction} - {plan.status}"
    lines = ["=" * len(header), header, "=" * len(header)]
    lines.append(f"Time: {plan.timestamp:%Y-%m-%d %H:%M:%S %Z}")
    lines.append(f"Setup: {plan.setup}")
    lines.append(f"Entry: {plan.entry_trigger}")
    lines.append(f"Invalidation: {plan.invalidation}")
    lines.append(f"Premium stop: {plan.premium_stop}")
    lines.append(f"Targets: {plan.targets}")
    if plan.option_contract:
        option = plan.option_contract
        lines.append(
            "Option: "
            f"{option.symbol} {option.expiration} {option.strike:.2f} {option.option_type} "
            f"(bid {option.bid:.2f} ask {option.ask:.2f} mid {option.mid:.2f})"
        )
    else:
        lines.append("Option: None")
    lines.append(f"Size: {plan.contracts} contracts")
    lines.append(f"Est. risk: ${plan.estimated_risk:.2f}")
    lines.append(f"Est. premium: ${plan.estimated_premium:.2f}")
    if plan.regime_info:
        lines.append(f"Regime: {plan.regime_info}")
    if plan.reject_reasons:
        lines.append("Reject reasons: " + "; ".join(plan.reject_reasons))
    else:
        lines.append("Reject reasons: None")
    if plan.warnings:
        lines.append("Warnings: " + "; ".join(plan.warnings))
    if plan.execution_status:
        message = f" ({plan.execution_message})" if plan.execution_message else ""
        lines.append(f"Execution: {plan.execution_status}{message}")
    lines.append("=" * len(header))
    return "\n".join(lines)
