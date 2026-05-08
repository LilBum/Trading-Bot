"""Load and session-segment futures bars for backtest replay.

Futures (CME e-minis ES/NQ) trade ~23 hours/day on Globex. The ORB strategy
itself anchors at 09:30 ET (config.json `orb.session_open_time` = "09:30")
— that's where the locked V2 NQ receipts came from. The session window
loaded here is wider (default 08:00-16:00 ET) so the signal engine has
warmup bars before the 09:30 anchor and continues to receive bars after
the range forms. Bars outside that window are dropped — they're either
overnight chop or after-hours, neither relevant to the cash-session ORB.
Holiday gaps fall out naturally because no bars appear on those dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


EASTERN = ZoneInfo("America/New_York")
DEFAULT_SESSION_START = time(8, 0)
DEFAULT_SESSION_END = time(16, 0)
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


@dataclass(frozen=True)
class FuturesTradingSession:
    """One day of bars from session_start through session_end (ET-localized)."""

    session_date: str   # YYYY-MM-DD in ET
    bars: pd.DataFrame  # ET-indexed DatetimeIndex; columns OHLCV


def load_bars_csv(path: Path | str) -> pd.DataFrame:
    """Load a Databento-format CSV into an ET-indexed OHLCV DataFrame."""
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


def filter_session_window(
    df: pd.DataFrame,
    session_start: time = DEFAULT_SESSION_START,
    session_end: time = DEFAULT_SESSION_END,
) -> pd.DataFrame:
    """Keep weekday bars between session_start (inclusive) and session_end (exclusive)."""
    if df.empty:
        return df
    is_weekday = df.index.weekday < 5
    bar_times = df.index.time
    in_window = (bar_times >= session_start) & (bar_times < session_end)
    return df[is_weekday & in_window]


def split_into_sessions(
    df: pd.DataFrame,
    session_start: time = DEFAULT_SESSION_START,
    session_end: time = DEFAULT_SESSION_END,
    min_bars: int = 60,
) -> list[FuturesTradingSession]:
    """Group windowed bars by ET calendar date.

    Sessions with fewer than `min_bars` bars (default 60 = 1 hour) are dropped
    as data quality casualties — early-2024 data sometimes has degraded days,
    and we don't want partial sessions corrupting backtest stats.
    """
    if df.empty:
        return []
    windowed = filter_session_window(df, session_start, session_end)
    if windowed.empty:
        return []
    sessions: list[FuturesTradingSession] = []
    for date_obj, group in windowed.groupby(windowed.index.date):
        if len(group) < min_bars:
            continue
        sessions.append(
            FuturesTradingSession(session_date=date_obj.isoformat(), bars=group)
        )
    return sessions


def load_sessions_for_symbol(
    symbol: str,
    data_dir: Path | str,
    session_start: time = DEFAULT_SESSION_START,
    session_end: time = DEFAULT_SESSION_END,
    min_bars: int = 60,
) -> list[FuturesTradingSession]:
    """Convenience: load `{data_dir}/{symbol}_1m.csv` and split into sessions."""
    path = Path(data_dir) / f"{symbol}_1m.csv"
    if not path.exists():
        raise FileNotFoundError(f"No bars file for {symbol} at {path}")
    df = load_bars_csv(path)
    return split_into_sessions(df, session_start, session_end, min_bars)
