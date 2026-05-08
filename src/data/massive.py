from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def _map_interval(interval: str) -> tuple[int, str]:
    value = interval.strip().lower()
    if value.endswith("m"):
        multiplier = int(value[:-1]) if value[:-1].isdigit() else 1
        return max(multiplier, 1), "minute"
    if value.endswith("h"):
        multiplier = int(value[:-1]) if value[:-1].isdigit() else 1
        return max(multiplier, 1), "hour"
    if value.endswith("d"):
        multiplier = int(value[:-1]) if value[:-1].isdigit() else 1
        return max(multiplier, 1), "day"
    if value in ("60", "1h"):
        return 1, "hour"
    return 1, "minute"


def _period_seconds(period: str) -> int:
    value = period.strip().lower()
    if value.endswith("d"):
        return int(value[:-1]) * 86400
    if value.endswith("mo"):
        return int(value[:-2]) * 30 * 86400
    if value.endswith("y"):
        return int(value[:-1]) * 365 * 86400
    return 86400


class MassiveBarsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        auth_mode: str = "query",
    ) -> None:
        if not api_key:
            raise RuntimeError("Missing Massive API key. Set massive.api_key or MASSIVE_API_KEY.")
        if not base_url:
            raise RuntimeError("Missing Massive base URL. Set massive.base_url.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode.lower()

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        return self._fetch_bars(symbol, period, interval, fallback_period="5d")

    def _fetch_bars(self, symbol: str, period: str, interval: str, fallback_period: str | None) -> pd.DataFrame:
        multiplier, timespan = _map_interval(interval)
        now = datetime.now(timezone.utc)
        lookback = _period_seconds(period)
        start = int((now.timestamp() - lookback) * 1000)
        end = int(now.timestamp() * 1000)

        url = f"{self.base_url}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{start}/{end}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}
        headers = {}
        if self.auth_mode == "header":
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["apiKey"] = self.api_key

        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        if status not in ("OK", "ok", "DELAYED", "delayed"):
            raise ValueError(f"Massive aggregate status: {status}")
        results = payload.get("results") or []
        if not results and fallback_period:
            if period != fallback_period:
                return self._fetch_bars(symbol, fallback_period, interval, fallback_period=None)
        if not results:
            raise ValueError("No intraday bars returned from Massive.")

        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime([item.get("t") for item in results], unit="ms", utc=True),
                "Open": [item.get("o") for item in results],
                "High": [item.get("h") for item in results],
                "Low": [item.get("l") for item in results],
                "Close": [item.get("c") for item in results],
                "Volume": [item.get("v") for item in results],
            }
        )
        df = df.set_index("timestamp").sort_index()
        df.index = df.index.tz_convert(EASTERN)
        return df[["Open", "High", "Low", "Close", "Volume"]]
