from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import os
import pandas as pd

from .finnhub import FinnhubBarsClient
from .massive import MassiveBarsClient
from .public_api import PublicApiClient, choose_expiration, quote_to_row
from .yahoo import YahooMarketDataProvider


EASTERN = ZoneInfo("America/New_York")


class PublicMarketDataProvider:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.client = PublicApiClient(config)
        self.yahoo = YahooMarketDataProvider()
        self._healthy = True
        self._last_error: str | None = None

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        bars_source = self.config.get("public", {}).get("bars_source", "yahoo")
        live_cfg = self.config.get("live_grade", {})
        live_enabled = live_cfg.get("enabled", False)
        fallback_list = live_cfg.get("bars_fallback") or []
        allow_yahoo = bool(live_cfg.get("allow_yahoo_fallback", False))
        def _allow_fallback(name: str) -> bool:
            if not live_enabled:
                return True
            if name == "yahoo":
                return allow_yahoo or name in fallback_list
            return name in fallback_list
        if bars_source == "massive":
            massive_cfg = self.config.get("massive", {})
            api_key = massive_cfg.get("api_key") or os.getenv("MASSIVE_API_KEY")
            base_url = massive_cfg.get("base_url")
            auth_mode = massive_cfg.get("auth_mode", "query")
            try:
                df = MassiveBarsClient(api_key, base_url=base_url, auth_mode=auth_mode).get_intraday_bars(
                    symbol, period, interval
                )
            except Exception:
                if _allow_fallback("finnhub"):
                    try:
                        finnhub_cfg = self.config.get("finnhub", {})
                        api_key = finnhub_cfg.get("api_key") or os.getenv("FINNHUB_API_KEY")
                        base_url = finnhub_cfg.get("base_url", "https://finnhub.io")
                        df = FinnhubBarsClient(api_key, base_url=base_url).get_intraday_bars(
                            symbol, period, interval
                        )
                    except Exception:
                        if _allow_fallback("yahoo"):
                            df = self.yahoo.get_intraday_bars(symbol, period, interval)
                        else:
                            raise
                elif _allow_fallback("yahoo"):
                    df = self.yahoo.get_intraday_bars(symbol, period, interval)
                else:
                    raise
        elif bars_source == "finnhub":
            finnhub_cfg = self.config.get("finnhub", {})
            api_key = finnhub_cfg.get("api_key") or os.getenv("FINNHUB_API_KEY")
            base_url = finnhub_cfg.get("base_url", "https://finnhub.io")
            try:
                df = FinnhubBarsClient(api_key, base_url=base_url).get_intraday_bars(symbol, period, interval)
            except Exception:
                if _allow_fallback("yahoo"):
                    df = self.yahoo.get_intraday_bars(symbol, period, interval)
                else:
                    raise
        else:
            df = self.yahoo.get_intraday_bars(symbol, period, interval)
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
        return df

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        instrument = {"symbol": symbol, "type": "EQUITY"}
        expirations_payload = self.client.get_option_expirations(instrument)
        expirations = expirations_payload.get("expirations") or []
        expiration = choose_expiration(expirations, target_dte, datetime.now(EASTERN))
        if not expiration:
            return None
        chain_payload = self.client.get_option_chain(instrument, expiration)
        calls = chain_payload.get("calls") or []
        puts = chain_payload.get("puts") or []
        if not calls and not puts:
            return None
        rows = [quote_to_row(symbol, quote, option_type="CALL") for quote in calls]
        rows.extend([quote_to_row(symbol, quote, option_type="PUT") for quote in puts])
        df = pd.DataFrame(rows)
        df["impliedVolatility"] = df.get("impliedVolatility", 0.0)
        self._populate_greeks(df, symbol)
        return expiration, df

    def health_check(self) -> bool:
        return self._healthy and self.yahoo.health_check()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _fetch_stock_quote(self, symbol: str) -> dict | None:
        payload = self.client.get_quotes([{"symbol": symbol, "type": "EQUITY"}])
        quotes = payload.get("quotes") or []
        if not quotes:
            return None
        return quotes[0]

    def _populate_greeks(self, df: pd.DataFrame, symbol: str) -> None:
        if df.empty:
            return
        public_cfg = self.config.get("public", {})
        if not public_cfg.get("use_greeks", True):
            return
        max_greeks = int(public_cfg.get("max_greeks", 250))
        if max_greeks <= 0:
            return

        quote = self._fetch_stock_quote(symbol)
        underlying = None
        if quote and quote.get("last"):
            try:
                underlying = float(quote.get("last"))
            except (TypeError, ValueError):
                underlying = None

        if underlying is not None and "strike" in df.columns:
            strike_diff = (df["strike"] - underlying).abs()
            symbols = (
                df.assign(_strike_diff=strike_diff)
                .sort_values("_strike_diff")["symbol"]
                .dropna()
                .astype(str)
                .head(max_greeks)
                .tolist()
            )
        else:
            symbols = df["symbol"].dropna().astype(str).head(max_greeks).tolist()
        if not symbols:
            return

        batch_size = int(public_cfg.get("greeks_batch_size", 50))
        if batch_size <= 0:
            return
        greeks_map: dict[str, dict] = {}
        for start in range(0, len(symbols), batch_size):
            batch = symbols[start : start + batch_size]
            try:
                greeks_payload = self.client.get_option_greeks(batch)
            except Exception:
                continue
            greeks_list = greeks_payload.get("greeks") or []
            for item in greeks_list:
                symbol_key = item.get("symbol")
                if symbol_key:
                    greeks_map[symbol_key] = item.get("greeks")
        for idx, row in df.iterrows():
            greeks = greeks_map.get(row.get("symbol"))
            if not greeks:
                continue
            implied = greeks.get("impliedVolatility")
            try:
                df.at[idx, "impliedVolatility"] = float(implied)
            except (TypeError, ValueError):
                continue
