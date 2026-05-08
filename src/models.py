from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class OptionGreeks:
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None


@dataclass
class OptionContract:
    symbol: str
    expiration: str
    strike: float
    option_type: str
    bid: float
    ask: float
    mid: float
    implied_volatility: float
    spread: float
    spread_pct: float
    nbbo_bid: float
    nbbo_ask: float
    open_interest: int
    volume: int
    last_price: float
    underlying_price: float
    time_to_expiry_days: float
    quote_time_utc: str
    greeks: OptionGreeks = field(default_factory=OptionGreeks)
    option_symbol: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SignalDecision:
    symbol: str
    direction: str
    setup: str
    entry_trigger: str
    invalidation: str
    premium_stop: str
    targets: str
    decision_time_utc: str
    bar_timestamp: datetime
    regime_info: Optional[str] = None
    atr_value: Optional[float] = None
    atr_pct: Optional[float] = None
    higher_timeframe_trend: Optional[str] = None
    sentiment_value: Optional[float] = None
    sentiment_label: Optional[str] = None
    sentiment_source: Optional[str] = None
    reject_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class RiskDecision:
    allowed: bool
    contracts: int
    estimated_risk: float
    estimated_premium: float
    risk_pct_base: Optional[float] = None
    risk_pct_used: Optional[float] = None
    atr_target_pct: Optional[float] = None
    stop_mode: Optional[str] = None
    risk_per_contract: Optional[float] = None
    reject_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class InstrumentSelection:
    option_contract: Optional[OptionContract]
    reject_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    top_candidates: List[dict] = field(default_factory=list)


@dataclass
class PlanResult:
    symbol: str
    timestamp: datetime
    setup: str
    direction: str
    entry_trigger: str
    invalidation: str
    premium_stop: str
    targets: str
    contracts: int
    estimated_risk: float
    estimated_premium: float
    option_contract: Optional[OptionContract]
    status: str
    reject_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    regime_info: Optional[str] = None
    decision_time_utc: Optional[str] = None
    run_id: Optional[str] = None
    decision_id: Optional[str] = None
    data_health_score: Optional[float] = None
    underlying_price: Optional[float] = None
    atr_value: Optional[float] = None
    atr_pct: Optional[float] = None
    higher_timeframe_trend: Optional[str] = None
    sentiment_value: Optional[float] = None
    sentiment_label: Optional[str] = None
    risk_pct_base: Optional[float] = None
    risk_pct_used: Optional[float] = None
    atr_target_pct: Optional[float] = None
    stop_mode: Optional[str] = None
    execution_status: Optional[str] = None
    execution_message: Optional[str] = None
    order_id: Optional[str] = None
    orb: Optional[dict] = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        if self.timestamp.tzinfo is None:
            payload["timestamp_utc"] = self.timestamp.replace(tzinfo=timezone.utc).isoformat()
        else:
            payload["timestamp_utc"] = self.timestamp.astimezone(timezone.utc).isoformat()
        if self.option_contract is None:
            payload["option_contract"] = None
        return payload
