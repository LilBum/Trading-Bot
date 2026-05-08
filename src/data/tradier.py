from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

import pandas as pd
import requests

from .public_api import choose_expiration


EASTERN = ZoneInfo("America/New_York")
TRADIER_BASE_URL = "https://api.tradier.com/v1"
TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1"


def _period_seconds(period: str) -> int:
    value = period.strip().lower()
    if value.endswith("d"):
        return int(value[:-1]) * 86400
    if value.endswith("mo"):
        return int(value[:-2]) * 30 * 86400
    if value.endswith("y"):
        return int(value[:-1]) * 365 * 86400
    return 86400


def _interval_to_tradier(interval: str) -> str:
    value = interval.strip().lower()
    if value.endswith("m") and value[:-1].isdigit():
        return f"{int(value[:-1])}min"
    if value in ("1h", "60", "60m"):
        return "60min"
    return "1min"


def _format_tradier_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class TradierApiClient:
    def __init__(self, config: dict) -> None:
        tradier_cfg = config.get("tradier", {})
        sandbox = bool(tradier_cfg.get("sandbox", False))
        token = tradier_cfg.get("access_token") or os.getenv("TRADIER_ACCESS_TOKEN")
        if sandbox:
            token = tradier_cfg.get("sandbox_token") or os.getenv("TRADIER_SANDBOX_TOKEN") or token
            base_url = tradier_cfg.get("sandbox_base_url") or tradier_cfg.get("base_url") or TRADIER_SANDBOX_URL
        else:
            base_url = tradier_cfg.get("base_url") or TRADIER_BASE_URL
        if not token:
            raise RuntimeError("Missing Tradier access token. Set tradier.access_token or TRADIER_ACCESS_TOKEN.")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = requests.get(
            f"{self.base_url}{path}",
            params=params or {},
            headers=self._headers(),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def get_quotes(self, symbols: list[str], greeks: bool = False) -> dict:
        params = {
            "symbols": ",".join(symbols),
            "greeks": "true" if greeks else "false",
        }
        return self._get("/markets/quotes", params=params)

    def get_timesales(self, symbol: str, start: str, end: str, interval: str) -> dict:
        params = {"symbol": symbol, "start": start, "end": end, "interval": interval}
        return self._get("/markets/timesales", params=params)

    def get_option_expirations(self, symbol: str) -> dict:
        return self._get("/markets/options/expirations", params={"symbol": symbol})

    def get_option_chain(self, symbol: str, expiration: str, greeks: bool = True) -> dict:
        params = {
            "symbol": symbol,
            "expiration": expiration,
            "greeks": "true" if greeks else "false",
        }
        return self._get("/markets/options/chains", params=params)


class TradierMarketDataProvider:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.client = TradierApiClient(config)
        self._healthy = True
        self._last_error: str | None = None

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        lookback = _period_seconds(period)
        now = datetime.now(EASTERN)
        start = now - timedelta(seconds=lookback)
        interval_param = _interval_to_tradier(interval)
        payload = self.client.get_timesales(
            symbol,
            _format_tradier_dt(start),
            _format_tradier_dt(now),
            interval_param,
        )
        items = self._extract_series(payload)
        if not items:
            raise ValueError("No intraday bars returned from Tradier.")

        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ts_raw = item.get("time") or item.get("timestamp") or item.get("date")
            if ts_raw is None:
                continue
            ts = pd.to_datetime(ts_raw, errors="coerce")
            if pd.isna(ts):
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize(EASTERN)
            else:
                ts = ts.tz_convert(EASTERN)

            open_val = item.get("open") or item.get("o")
            high_val = item.get("high") or item.get("h")
            low_val = item.get("low") or item.get("l")
            close_val = item.get("close") or item.get("c")
            if open_val is None and high_val is None and low_val is None and close_val is None:
                price = item.get("price") or item.get("last")
                open_val = high_val = low_val = close_val = price

            rows.append(
                {
                    "timestamp": ts,
                    "Open": _safe_float(open_val),
                    "High": _safe_float(high_val),
                    "Low": _safe_float(low_val),
                    "Close": _safe_float(close_val),
                    "Volume": _safe_int(item.get("volume") or item.get("v")),
                }
            )

        if not rows:
            raise ValueError("No intraday bars returned from Tradier.")
        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        quote = self._fetch_stock_quote(symbol)
        if quote is not None:
            last_price = quote.get("last")
            if last_price:
                df = df.copy()
                df.iloc[-1, df.columns.get_loc("Close")] = float(last_price)
                df.iloc[-1, df.columns.get_loc("High")] = max(df.iloc[-1]["High"], float(last_price))
                df.iloc[-1, df.columns.get_loc("Low")] = min(df.iloc[-1]["Low"], float(last_price))

        self._healthy = True
        self._last_error = None
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        expirations_payload = self.client.get_option_expirations(symbol)
        expirations = self._parse_expirations(expirations_payload)
        expiration = choose_expiration(expirations, target_dte, datetime.now(EASTERN))
        if not expiration:
            return None

        chain_payload = self.client.get_option_chain(symbol, expiration, greeks=True)
        options = self._parse_option_chain(chain_payload)
        if not options:
            return None
        fetched_at = datetime.now(timezone.utc).isoformat()
        rows = [self._option_to_row(symbol, option, fetched_at) for option in options]
        df = pd.DataFrame(rows)
        if "impliedVolatility" not in df.columns:
            df["impliedVolatility"] = 0.0
        self._healthy = True
        self._last_error = None
        return expiration, df

    def health_check(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _fetch_stock_quote(self, symbol: str) -> dict | None:
        payload = self.client.get_quotes([symbol], greeks=False)
        quotes = self._parse_quotes(payload)
        if not quotes:
            return None
        return quotes[0]

    def _parse_quotes(self, payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        quotes = payload.get("quotes") or {}
        if isinstance(quotes, dict):
            quote = quotes.get("quote")
            return _as_list(quote)
        return _as_list(quotes)

    def _parse_expirations(self, payload: dict) -> list[str]:
        if not isinstance(payload, dict):
            return []
        expirations = payload.get("expirations") or payload.get("expiration") or {}
        if isinstance(expirations, dict):
            dates = expirations.get("date") or expirations.get("dates")
            return _as_list(dates)
        return _as_list(expirations)

    def _parse_option_chain(self, payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        options = payload.get("options") or payload.get("option") or {}
        if isinstance(options, dict):
            return _as_list(options.get("option") or options.get("options") or options.get("data"))
        return _as_list(options)

    def _extract_series(self, payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        for key in ("series", "timesales", "data", "history"):
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                data = value.get("data") or value.get("series") or value.get("timesale")
                if data is not None:
                    return _as_list(data)
            if isinstance(value, list):
                return value
        return []

    def _option_to_row(self, symbol: str, option: dict, fetched_at: str) -> dict:
        option_type = option.get("option_type") or option.get("type") or option.get("optionType")
        if isinstance(option_type, str):
            opt_upper = option_type.upper()
            if opt_upper.startswith("C"):
                option_type = "CALL"
            elif opt_upper.startswith("P"):
                option_type = "PUT"
            else:
                option_type = opt_upper
        else:
            option_type = "CALL"

        greeks = option.get("greeks") or {}
        implied_vol = (
            greeks.get("mid_iv")
            or greeks.get("iv")
            or option.get("implied_volatility")
            or option.get("impliedVolatility")
        )
        quote_time = (
            option.get("trade_date")
            or option.get("last_trade_date")
            or option.get("timestamp")
            or option.get("quote_time")
            or fetched_at
        )
        option_symbol = option.get("option_symbol") or option.get("optionSymbol")
        raw_symbol = option.get("symbol")
        if option_symbol is None and isinstance(raw_symbol, str) and any(ch.isdigit() for ch in raw_symbol):
            option_symbol = raw_symbol
        return {
            "symbol": symbol,
            "option_symbol": option_symbol,
            "expiration": option.get("expiration_date") or option.get("expiration") or option.get("exp_date"),
            "strike": _safe_float(option.get("strike")),
            "option_type": option_type,
            "bid": _safe_float(option.get("bid")),
            "ask": _safe_float(option.get("ask")),
            "last_price": _safe_float(option.get("last") or option.get("last_price")),
            "open_interest": _safe_int(option.get("open_interest") or option.get("openInterest")),
            "volume": _safe_int(option.get("volume")),
            "bidSize": _safe_int(option.get("bid_size") or option.get("bidSize")),
            "askSize": _safe_int(option.get("ask_size") or option.get("askSize")),
            "lastTradeDate": quote_time,
            "impliedVolatility": _safe_float(implied_vol),
            "delta": _safe_float(greeks.get("delta")),
            "gamma": _safe_float(greeks.get("gamma")),
            "theta": _safe_float(greeks.get("theta")),
            "vega": _safe_float(greeks.get("vega")),
            "rho": _safe_float(greeks.get("rho")),
        }
