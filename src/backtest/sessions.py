"""Load and session-segment historical 1-minute bars for backtest replay.

Live planner code computes VWAP cumulatively from the first bar of the
input. That is correct intraday only if the input is a single trading
session. Multi-day backtests must therefore feed bars one session at a
time, with VWAP resetting at each market open. This module produces those
session slices from CSV output of `scripts/download_historical.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


EASTERN = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


@dataclass(frozen=True)
class TradingSession:
    """One day of Regular-Trading-Hours bars, ET-indexed."""

    session_date: str   # YYYY-MM-DD in ET
    bars: pd.DataFrame  # ET-indexed DatetimeIndex; columns OHLCV


def load_bars_csv(path: Path | str) -> pd.DataFrame:
    """Load a Massive-format CSV into an ET-indexed OHLCV DataFrame."""
    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        raise ValueError(f"{path}: missing 'timestamp' column")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df.index = df.index.tz_convert(EASTERN)
    return df[list(REQUIRED_COLUMNS)]


def filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only weekday bars between 09:30 (inclusive) and 16:00 (exclusive) ET."""
    if df.empty:
        return df
    is_weekday = df.index.weekday < 5
    bar_times = df.index.time
    in_rth = (bar_times >= RTH_OPEN) & (bar_times < RTH_CLOSE)
    return df[is_weekday & in_rth]


def split_into_sessions(df: pd.DataFrame) -> list[TradingSession]:
    """Group bars by ET calendar date; return a list of TradingSession."""
    if df.empty:
        return []
    rth = filter_rth(df)
    if rth.empty:
        return []
    sessions: list[TradingSession] = []
    for date_obj, group in rth.groupby(rth.index.date):
        if group.empty:
            continue
        sessions.append(
            TradingSession(session_date=date_obj.isoformat(), bars=group)
        )
    return sessions


def load_sessions_for_symbol(
    symbol: str,
    data_dir: Path | str,
) -> list[TradingSession]:
    """Convenience: load `{data_dir}/{symbol}_1m.csv` and split into sessions."""
    path = Path(data_dir) / f"{symbol}_1m.csv"
    if not path.exists():
        raise FileNotFoundError(f"No bars file for {symbol} at {path}")
    df = load_bars_csv(path)
    return split_into_sessions(df)
