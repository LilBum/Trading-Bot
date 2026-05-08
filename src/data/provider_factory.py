from __future__ import annotations

from typing import Any

from .public_provider import PublicMarketDataProvider
from .tradier import TradierMarketDataProvider
from .webull import HybridMarketDataProvider, WebullMarketDataProvider
from .yahoo import YahooMarketDataProvider


def create_market_data_provider(config: dict) -> tuple[Any, str | None]:
    provider_name = config.get("data_provider", "yahoo").lower()
    if provider_name == "webull":
        return WebullMarketDataProvider(config), "webull"
    if provider_name == "hybrid":
        return HybridMarketDataProvider(config), "hybrid"
    if provider_name == "public":
        return PublicMarketDataProvider(config), "public"
    if provider_name == "tradier":
        return TradierMarketDataProvider(config), "tradier"
    return YahooMarketDataProvider(), "yahoo"
