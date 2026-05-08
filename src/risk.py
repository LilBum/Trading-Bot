import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import OptionContract, RiskDecision


def calculate_position_size(
    account_equity: float,
    risk_pct: float,
    option_mid: float,
    premium_stop_pct: float,
    max_premium_per_trade: float,
    max_contracts: int,
    stop_mode: str = "premium_pct",
    atr_value: float | None = None,
    delta: float | None = None,
    atr_stop_multiplier: float = 1.0,
) -> dict:
    if option_mid <= 0:
        return {
            "contracts": 0,
            "estimated_risk": 0.0,
            "estimated_premium": 0.0,
            "reason": "Option mid must be positive",
        }

    if stop_mode == "delta_atr":
        if atr_value is None or delta is None or delta == 0:
            return {
                "contracts": 0,
                "estimated_risk": 0.0,
                "estimated_premium": 0.0,
                "reason": "ATR stop requires delta and ATR",
            }
        risk_per_contract = abs(delta) * atr_value * 100.0 * atr_stop_multiplier
    else:
        risk_per_contract = option_mid * 100.0 * premium_stop_pct
    premium_per_contract = option_mid * 100.0

    if risk_per_contract <= 0 or premium_per_contract <= 0:
        return {
            "contracts": 0,
            "estimated_risk": 0.0,
            "estimated_premium": 0.0,
            "reason": "Invalid premium stop sizing",
        }

    base_contracts = math.floor((account_equity * risk_pct) / risk_per_contract)
    if base_contracts <= 0:
        return {
            "contracts": 0,
            "estimated_risk": 0.0,
            "estimated_premium": 0.0,
            "reason": "Risk budget too small",
        }

    max_by_premium = math.floor(max_premium_per_trade / premium_per_contract)
    contracts = min(base_contracts, max_by_premium, max_contracts)

    if contracts <= 0:
        return {
            "contracts": 0,
            "estimated_risk": 0.0,
            "estimated_premium": 0.0,
            "reason": "Position capped to zero",
        }

    return {
        "contracts": contracts,
        "estimated_risk": contracts * risk_per_contract,
        "estimated_premium": contracts * premium_per_contract,
        "risk_per_contract": risk_per_contract,
        "reason": None,
    }


class RiskEngine:
    def __init__(self) -> None:
        self._last_signal_time: dict[tuple[str, str], datetime] = {}
        self._session_date: str | None = None
        self._allowed_count = 0
        self._allowed_contracts_total = 0
        self._allowed_contracts_by_symbol: dict[str, int] = {}
        self._recent_signatures: dict[str, datetime] = {}

    def reset_counters(self) -> None:
        self._session_date = None
        self._allowed_count = 0
        self._allowed_contracts_total = 0
        self._allowed_contracts_by_symbol = {}
        self._recent_signatures = {}
        self._last_signal_time = {}

    def assess(
        self,
        symbol: str,
        option_contract: OptionContract | None,
        decision_time_utc: str,
        direction: str,
        config: dict,
        atr_value: float | None = None,
        atr_pct: float | None = None,
    ) -> RiskDecision:
        reject_reasons: list[str] = []
        warnings: list[str] = []

        risk_controls = config.get("risk_controls", {})
        if risk_controls.get("kill_switch", False):
            self._flag_control("kill_switch", "Kill switch active", reject_reasons, warnings, config)
        if risk_controls.get("block_on_data_error") and not config.get("runtime", {}).get("data_health_ok", True):
            self._flag_control(
                "data_unhealthy",
                "Data provider unhealthy",
                reject_reasons,
                warnings,
                config,
            )
        self._apply_event_risk(reject_reasons, warnings, config)

        if option_contract is None:
            reject_reasons.append("Missing option contract for sizing")
            return RiskDecision(
                allowed=False,
                contracts=0,
                estimated_risk=0.0,
                estimated_premium=0.0,
                reject_reasons=reject_reasons,
                warnings=warnings,
            )

        account_cfg = config.get("account", {})
        sizing_cfg = config.get("position_sizing", {})

        base_risk_pct = account_cfg.get("risk_pct", 0.0)
        risk_pct_used, atr_target_pct = self._adjust_risk_pct(base_risk_pct, atr_pct, sizing_cfg, warnings)

        stop_mode = sizing_cfg.get("stop_mode", "premium_pct")
        atr_stop_multiplier = sizing_cfg.get("atr_stop_multiplier", 1.0)
        fallback_to_premium = sizing_cfg.get("fallback_to_premium_stop", True)
        if stop_mode == "delta_atr" and (atr_value is None or option_contract.greeks.delta is None):
            if fallback_to_premium:
                warnings.append("ATR stop unavailable; using premium stop")
                stop_mode = "premium_pct"
            else:
                reject_reasons.append("ATR stop requires delta and ATR")
                return RiskDecision(
                    allowed=False,
                    contracts=0,
                    estimated_risk=0.0,
                    estimated_premium=0.0,
                    risk_pct_base=base_risk_pct,
                    risk_pct_used=risk_pct_used,
                    atr_target_pct=atr_target_pct,
                    stop_mode=stop_mode,
                    reject_reasons=reject_reasons,
                    warnings=warnings,
                )

        max_premium = sizing_cfg.get("max_premium_per_trade")
        if max_premium is None:
            max_premium = account_cfg.get("equity", 0.0)
        max_contracts = sizing_cfg.get("max_contracts", 0)
        sizing = calculate_position_size(
            account_cfg.get("equity", 0.0),
            risk_pct_used,
            option_contract.mid,
            sizing_cfg.get("premium_stop_pct", 0.25),
            max_premium,
            max_contracts,
            stop_mode=stop_mode,
            atr_value=atr_value,
            delta=option_contract.greeks.delta,
            atr_stop_multiplier=atr_stop_multiplier,
        )

        if sizing["contracts"] <= 0:
            if sizing.get("reason"):
                reject_reasons.append(sizing["reason"])
            return RiskDecision(
                allowed=False,
                contracts=0,
                estimated_risk=0.0,
                estimated_premium=0.0,
                risk_pct_used=risk_pct_used,
                stop_mode=stop_mode,
                reject_reasons=reject_reasons,
                warnings=warnings,
            )

        contracts = sizing["contracts"]
        estimated_risk = sizing["estimated_risk"]
        estimated_premium = sizing["estimated_premium"]
        risk_per_contract = sizing.get("risk_per_contract")

        max_mid_to_last = risk_controls.get("max_mid_to_last_price_deviation_pct")
        if max_mid_to_last is not None and option_contract.last_price > 0 and option_contract.mid > 0:
            deviation = abs(option_contract.last_price - option_contract.mid) / option_contract.mid * 100.0
            if deviation > max_mid_to_last:
                self._flag_control(
                    "price_deviation",
                    "Price deviation exceeds tolerance",
                    reject_reasons,
                    warnings,
                    config,
                )

        max_notional = risk_controls.get("max_notional_per_order")
        if max_notional is not None and estimated_premium > max_notional:
            self._flag_control(
                "max_notional_per_order",
                "Order notional exceeds limit",
                reject_reasons,
                warnings,
                config,
            )

        max_contracts = risk_controls.get("max_contracts_per_order")
        if max_contracts is not None and contracts > max_contracts:
            self._flag_control(
                "max_contracts_per_order",
                "Contract count exceeds limit",
                reject_reasons,
                warnings,
                config,
            )

        max_position_per_symbol = risk_controls.get("max_position_per_symbol")
        if max_position_per_symbol is not None:
            existing = self._allowed_contracts_by_symbol.get(symbol, 0)
            if existing + contracts > max_position_per_symbol:
                self._flag_control(
                    "max_position_per_symbol",
                    "Max position per symbol exceeded",
                    reject_reasons,
                    warnings,
                    config,
                )

        max_total_contracts = risk_controls.get("max_total_contracts_per_day")
        if max_total_contracts is not None and self._allowed_contracts_total + contracts > max_total_contracts:
            self._flag_control(
                "max_total_contracts_per_day",
                "Max total contracts per day exceeded",
                reject_reasons,
                warnings,
                config,
            )

        self._apply_throttle(symbol, direction, decision_time_utc, reject_reasons, warnings, config)
        self._apply_duplicate_detection(option_contract, decision_time_utc, reject_reasons, warnings, config)
        self._apply_trade_count_limit(reject_reasons, warnings, config)
        self._apply_daily_loss_lockout(decision_time_utc, reject_reasons, warnings, config)
        self._apply_cooldown(decision_time_utc, reject_reasons, warnings, config)

        allowed = len(reject_reasons) == 0
        if allowed:
            self._record_allowed(decision_time_utc, symbol, direction, contracts, option_contract)

        return RiskDecision(
            allowed=allowed,
            contracts=contracts if allowed else 0,
            estimated_risk=estimated_risk if allowed else 0.0,
            estimated_premium=estimated_premium if allowed else 0.0,
            risk_pct_base=base_risk_pct,
            risk_pct_used=risk_pct_used,
            atr_target_pct=atr_target_pct,
            stop_mode=stop_mode,
            risk_per_contract=risk_per_contract,
            reject_reasons=reject_reasons,
            warnings=warnings,
        )

    def _adjust_risk_pct(
        self,
        base_risk_pct: float,
        atr_pct: float | None,
        sizing_cfg: dict,
        warnings: list[str],
    ) -> tuple[float, float | None]:
        adjustment_cfg = sizing_cfg.get("volatility_adjustment", {})
        if not adjustment_cfg.get("enabled", False):
            return base_risk_pct, None
        if atr_pct is None or atr_pct <= 0:
            warnings.append("ATR unavailable for volatility sizing")
            return base_risk_pct, adjustment_cfg.get("atr_target_pct")
        target = adjustment_cfg.get("atr_target_pct")
        if not target:
            return base_risk_pct, None
        adjusted = base_risk_pct * (float(target) / atr_pct)
        min_pct = adjustment_cfg.get("min_risk_pct")
        max_pct = adjustment_cfg.get("max_risk_pct")
        if min_pct is not None:
            adjusted = max(adjusted, float(min_pct))
        if max_pct is not None:
            adjusted = min(adjusted, float(max_pct))
        return adjusted, float(target)

    def _apply_throttle(
        self,
        symbol: str,
        direction: str,
        decision_time_utc: str,
        reject_reasons: list[str],
        warnings: list[str],
        config: dict,
    ) -> None:
        throttle_seconds = config.get("risk_controls", {}).get("min_seconds_between_signals", 0)
        if throttle_seconds <= 0:
            return
        try:
            decision_dt = datetime.fromisoformat(decision_time_utc).astimezone(timezone.utc)
        except ValueError:
            decision_dt = datetime.now(timezone.utc)
        key = (symbol, direction)
        last_time = self._last_signal_time.get(key)
        if last_time and (decision_dt - last_time).total_seconds() < throttle_seconds:
            self._flag_control(
                "throttle",
                "Signal throttle: too soon since last plan",
                reject_reasons,
                warnings,
                config,
            )

    def _apply_trade_count_limit(self, reject_reasons: list[str], warnings: list[str], config: dict) -> None:
        max_trades = config.get("account", {}).get("max_trades_per_day")
        if max_trades is None:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if self._session_date != today:
            self._session_date = today
            self._allowed_count = 0
            self._allowed_contracts_total = 0
            self._allowed_contracts_by_symbol = {}
            self._recent_signatures = {}
        if self._allowed_count >= max_trades:
            self._flag_control(
                "max_trades_per_day",
                "Max trades per day reached",
                reject_reasons,
                warnings,
                config,
            )

    def _apply_daily_loss_lockout(
        self,
        decision_time_utc: str,
        reject_reasons: list[str],
        warnings: list[str],
        config: dict,
    ) -> None:
        account_cfg = config.get("account", {})
        max_daily_loss_pct = account_cfg.get("max_daily_loss_pct")
        if max_daily_loss_pct is None:
            return
        daily_state = config.get("runtime", {}).get("daily_state", {})
        realized_pnl = daily_state.get("realized_pnl", 0.0)
        if realized_pnl <= -(account_cfg.get("equity", 0.0) * max_daily_loss_pct):
            self._flag_control(
                "daily_loss_limit",
                "Daily loss limit reached",
                reject_reasons,
                warnings,
                config,
            )

    def _apply_cooldown(
        self,
        decision_time_utc: str,
        reject_reasons: list[str],
        warnings: list[str],
        config: dict,
    ) -> None:
        cooldown_minutes = config.get("account", {}).get("cooldown_minutes_after_loss")
        if cooldown_minutes is None:
            return
        daily_state = config.get("runtime", {}).get("daily_state", {})
        last_loss_time = daily_state.get("last_loss_time_utc")
        if not last_loss_time:
            return
        try:
            loss_dt = datetime.fromisoformat(last_loss_time).astimezone(timezone.utc)
        except ValueError:
            return
        try:
            decision_dt = datetime.fromisoformat(decision_time_utc).astimezone(timezone.utc)
        except ValueError:
            decision_dt = datetime.now(timezone.utc)
        if (decision_dt - loss_dt).total_seconds() < cooldown_minutes * 60.0:
            self._flag_control(
                "cooldown_after_loss",
                "Cooldown after loss active",
                reject_reasons,
                warnings,
                config,
            )

    def _apply_duplicate_detection(
        self,
        option_contract: OptionContract,
        decision_time_utc: str,
        reject_reasons: list[str],
        warnings: list[str],
        config: dict,
    ) -> None:
        window_seconds = config.get("risk_controls", {}).get("duplicate_order_window_seconds", 0)
        if window_seconds <= 0:
            return
        signature = f"{option_contract.symbol}|{option_contract.option_type}|{option_contract.expiration}|{option_contract.strike:.2f}"
        try:
            decision_dt = datetime.fromisoformat(decision_time_utc).astimezone(timezone.utc)
        except ValueError:
            decision_dt = datetime.now(timezone.utc)
        last_time = self._recent_signatures.get(signature)
        if last_time and (decision_dt - last_time).total_seconds() < window_seconds:
            self._flag_control(
                "duplicate_order",
                "Duplicate order detected",
                reject_reasons,
                warnings,
                config,
            )

    def _record_allowed(
        self,
        decision_time_utc: str,
        symbol: str,
        direction: str,
        contracts: int,
        option_contract: OptionContract,
    ) -> None:
        try:
            decision_dt = datetime.fromisoformat(decision_time_utc).astimezone(timezone.utc)
        except ValueError:
            decision_dt = datetime.now(timezone.utc)
        key = (symbol, direction)
        self._last_signal_time[key] = decision_dt
        today = decision_dt.date().isoformat()
        if self._session_date != today:
            self._session_date = today
            self._allowed_count = 0
            self._allowed_contracts_total = 0
            self._allowed_contracts_by_symbol = {}
        self._allowed_count += 1
        self._allowed_contracts_total += contracts
        self._allowed_contracts_by_symbol[symbol] = self._allowed_contracts_by_symbol.get(symbol, 0) + contracts
        signature = f"{option_contract.symbol}|{option_contract.option_type}|{option_contract.expiration}|{option_contract.strike:.2f}"
        self._recent_signatures[signature] = decision_dt

    def _apply_event_risk(
        self,
        reject_reasons: list[str],
        warnings: list[str],
        config: dict,
    ) -> None:
        dates = config.get("risk_controls", {}).get("event_risk_dates") or []
        if not dates:
            return
        eastern = ZoneInfo("America/New_York")
        today = datetime.now(eastern).date().isoformat()
        if today in dates:
            self._flag_control(
                "event_risk",
                "Event risk date: trading blocked",
                reject_reasons,
                warnings,
                config,
            )

    def _flag_control(
        self,
        code: str,
        message: str,
        reject_reasons: list[str],
        warnings: list[str],
        config: dict,
    ) -> None:
        levels = config.get("risk_controls", {}).get("control_levels", {})
        level = str(levels.get(code, "hard")).lower()
        if level == "warn":
            warnings.append(message)
        else:
            reject_reasons.append(message)
