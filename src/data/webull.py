from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .yahoo import YahooMarketDataProvider

try:
    from webull.core.client import ApiClient
    from webull.data.common.category import Category
    from webull.data.common.timespan import Timespan
    from webull.data.data_client import DataClient
except ImportError:  # pragma: no cover - optional dependency
    ApiClient = None
    Category = None
    Timespan = None
    DataClient = None


EASTERN = ZoneInfo("America/New_York")


def _extract_bars(payload: object) -> list[dict] | list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "bars", "barList", "items", "data_list", "dataList"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for inner_key in ("bars", "barList", "items", "data", "dataList"):
                inner_value = value.get(inner_key)
                if isinstance(inner_value, list):
                    return inner_value
    return []


def _parse_bar_timestamp(value: object) -> pd.Timestamp | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        unit = "ms" if value > 1_000_000_000_000 else "s"
        return pd.to_datetime(value, unit=unit, utc=True, errors="coerce")
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts


def _bars_to_dataframe(bars: list) -> pd.DataFrame:
    rows: list[dict] = []
    for bar in bars:
        if isinstance(bar, (list, tuple)) and len(bar) >= 6:
            ts = _parse_bar_timestamp(bar[0])
            open_px, high_px, low_px, close_px, volume = bar[1:6]
        elif isinstance(bar, dict):
            ts = _parse_bar_timestamp(bar.get("time") or bar.get("timestamp") or bar.get("t"))
            open_px = bar.get("open") or bar.get("o")
            high_px = bar.get("high") or bar.get("h")
            low_px = bar.get("low") or bar.get("l")
            close_px = bar.get("close") or bar.get("c")
            volume = bar.get("volume") or bar.get("v")
        else:
            continue
        if ts is None or pd.isna(ts):
            continue
        rows.append(
            {
                "timestamp": ts,
                "Open": float(open_px) if open_px is not None else 0.0,
                "High": float(high_px) if high_px is not None else 0.0,
                "Low": float(low_px) if low_px is not None else 0.0,
                "Close": float(close_px) if close_px is not None else 0.0,
                "Volume": float(volume) if volume is not None else 0.0,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    df.index = df.index.tz_convert(EASTERN)
    return df


class WebullMarketDataProvider:
    def __init__(self, config: dict) -> None:
        self.config = config
        self._client: DataClient | None = None
        self._healthy = False
        self._last_error: str | None = None

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        client = self._ensure_client()
        timespan = self._map_timespan(interval)
        response = client.market_data.get_history_bar(symbol, Category.US_STOCK.name, timespan)
        payload = response.json()
        bars = _extract_bars(payload)
        df = _bars_to_dataframe(bars)
        if df.empty:
            self._healthy = False
            self._last_error = "No bars returned from Webull."
            raise ValueError(self._last_error)
        self._healthy = True
        self._last_error = None
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        raise RuntimeError("Webull options chain not available via API. Use hybrid provider.")

    def health_check(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _ensure_client(self) -> DataClient:
        if self._client is not None:
            return self._client
        if ApiClient is None or DataClient is None:
            raise RuntimeError(
                "Webull SDK not installed. Add webull-openapi-python-sdk to requirements.txt."
            )
        cfg = self.config.get("webull", {})
        app_key = cfg.get("app_key") or os.getenv("WEBULL_APP_KEY")
        app_secret = cfg.get("app_secret") or os.getenv("WEBULL_APP_SECRET")
        region = cfg.get("region", "us")
        if not app_key or not app_secret:
            raise RuntimeError("Missing Webull app_key/app_secret. Set in config or env.")

        api_client = ApiClient(app_key, app_secret, region)
        api_endpoint = cfg.get("api_endpoint")
        if api_endpoint:
            api_client.add_endpoint(region, api_endpoint)
        self._client = DataClient(api_client)
        return self._client

    def _map_timespan(self, interval: str) -> str:
        mapping = {
            "1m": "M1",
            "5m": "M5",
            "15m": "M15",
            "30m": "M30",
            "60m": "H1",
            "1h": "H1",
            "1d": "D1",
        }
        code = mapping.get(interval, "M1")
        if Timespan is None:
            return code
        if hasattr(Timespan, code):
            return getattr(Timespan, code).name
        return code


class HybridMarketDataProvider:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.webull = WebullMarketDataProvider(config)
        self.yahoo = YahooMarketDataProvider()
        self._healthy = True
        self._last_error: str | None = None

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        try:
            df = self.webull.get_intraday_bars(symbol, period, interval)
            self._healthy = True
            self._last_error = None
            return df
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)
            raise

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        try:
            chain = self.yahoo.get_options_chain(symbol, target_dte)
            self._healthy = True
            self._last_error = None
            return chain
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)
            raise

    def health_check(self) -> bool:
        return self._healthy and self.webull.health_check() and self.yahoo.health_check()

    @property
    def last_error(self) -> str | None:
        return self._last_error
