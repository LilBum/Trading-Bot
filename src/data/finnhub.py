from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def _resolution_from_interval(interval: str) -> str:
    mapping = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1h": "60",
        "1d": "D",
    }
    return mapping.get(interval, "1")


def _period_seconds(period: str) -> int:
    value = period.strip().lower()
    if value.endswith("d"):
        return int(value[:-1]) * 86400
    if value.endswith("mo"):
        return int(value[:-2]) * 30 * 86400
    if value.endswith("y"):
        return int(value[:-1]) * 365 * 86400
    return 86400


class FinnhubBarsClient:
    def __init__(self, api_key: str, base_url: str = "https://finnhub.io") -> None:
        if not api_key:
            raise RuntimeError("Missing Finnhub API key. Set finnhub.api_key or FINNHUB_API_KEY.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        resolution = _resolution_from_interval(interval)
        now = datetime.now(timezone.utc)
        lookback = _period_seconds(period)
        start = int((now.timestamp() - lookback))
        end = int(now.timestamp())
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "from": start,
            "to": end,
            "token": self.api_key,
        }
        response = requests.get(f"{self.base_url}/api/v1/stock/candle", params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if payload.get("s") != "ok":
            raise ValueError(f"Finnhub candle status: {payload.get('s')}")
        timestamps = payload.get("t") or []
        if not timestamps:
            raise ValueError("No intraday bars returned from Finnhub.")
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
                "Open": payload.get("o", []),
                "High": payload.get("h", []),
                "Low": payload.get("l", []),
                "Close": payload.get("c", []),
                "Volume": payload.get("v", []),
            }
        )
        df = df.set_index("timestamp").sort_index()
        df.index = df.index.tz_convert(EASTERN)
        return df[["Open", "High", "Low", "Close", "Volume"]]
