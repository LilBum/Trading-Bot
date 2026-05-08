from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


EASTERN = ZoneInfo("America/New_York")


def fetch_intraday_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False, prepost=False)
    if df.empty and period == "1d":
        df = ticker.history(period="5d", interval=interval, auto_adjust=False, prepost=False)
    if df.empty:
        raise ValueError(
            f"No intraday data returned for {symbol}. Try market hours or set history_period to 5d."
        )
    df = df.reset_index()
    ts_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={ts_col: "timestamp"})
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    ts = ts.dt.tz_convert(EASTERN)
    df["timestamp"] = ts
    df = df.set_index("timestamp")
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_options_chain(symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
    ticker = yf.Ticker(symbol)
    expirations = ticker.options
    if not expirations:
        return None
    today = datetime.now(EASTERN).date()
    target_date = today + timedelta(days=target_dte)
    exp_dates = [datetime.strptime(item, "%Y-%m-%d").date() for item in expirations]
    future_dates = [item for item in exp_dates if item >= target_date]
    if future_dates:
        expiration = min(future_dates)
    else:
        expiration = min(exp_dates, key=lambda item: abs((item - target_date).days))
    expiration_str = expiration.strftime("%Y-%m-%d")
    chain = ticker.option_chain(expiration_str)
    calls = chain.calls.copy()
    puts = chain.puts.copy()
    calls["option_type"] = "CALL"
    puts["option_type"] = "PUT"
    combined = pd.concat([calls, puts], ignore_index=True)
    combined = combined.rename(
        columns={
            "openInterest": "open_interest",
            "lastPrice": "last_price",
        }
    )
    combined["expiration"] = expiration_str
    return expiration_str, combined


class YahooMarketDataProvider:
    def __init__(self) -> None:
        self._healthy = True
        self._last_error: str | None = None

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        try:
            df = fetch_intraday_bars(symbol, period, interval)
            self._healthy = True
            self._last_error = None
            return df
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)
            raise

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        try:
            chain = fetch_options_chain(symbol, target_dte)
            self._healthy = True
            self._last_error = None
            return chain
        except Exception as exc:
            self._healthy = False
            self._last_error = str(exc)
            raise

    def health_check(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error
