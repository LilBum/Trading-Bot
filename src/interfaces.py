from __future__ import annotations

from typing import Protocol

import pandas as pd

from .models import InstrumentSelection, RiskDecision, SignalDecision


class MarketDataProvider(Protocol):
    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        ...

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        ...

    def health_check(self) -> bool:
        ...


class InstrumentService(Protocol):
    def select_contract(
        self,
        symbol: str,
        chain_data: tuple[str, pd.DataFrame] | None,
        direction: str,
        underlying_price: float,
        decision_time_utc: str,
    ) -> InstrumentSelection:
        ...


class SignalEngine(Protocol):
    def evaluate(self, symbol: str, df_1m: pd.DataFrame, config: dict) -> SignalDecision:
        ...


class RiskEngine(Protocol):
    def assess(
        self,
        symbol: str,
        option_contract,
        decision_time_utc: str,
        direction: str,
        config: dict,
    ) -> RiskDecision:
        ...


class OrderManager(Protocol):
    def cancel_all(self) -> None:
        ...


class ExecutionAdapter(Protocol):
    def submit_order(self, order_payload: dict) -> dict:
        ...


class Journal(Protocol):
    def log_event(self, event_type: str, payload: dict) -> None:
        ...
