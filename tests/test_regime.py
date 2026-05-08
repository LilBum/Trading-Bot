from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.regime import count_vwap_crosses, time_filter_allows


def test_count_vwap_crosses():
    close = pd.Series([0.9, 1.1, 0.9, 1.1, 0.9])
    vwap = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0])
    assert count_vwap_crosses(close, vwap, lookback=4) == 4


def test_count_vwap_crosses_skips_nan_bars():
    # Mimics zero-volume bars at session open where VWAP is NaN.
    close = pd.Series([100.0, 101.0, 99.0, 102.0])
    vwap = pd.Series([np.nan, 100.0, 100.0, 100.0])
    # NaN bar is sign 0 and skipped; remaining is +1, -1, +1 → 2 crosses.
    assert count_vwap_crosses(close, vwap, lookback=3) == 2


def test_count_vwap_crosses_carries_sign_across_zero_gap():
    # A bar exactly at VWAP shouldn't break cross detection across it.
    close = pd.Series([101.0, 100.0, 99.0])  # signs: +1, 0, -1
    vwap = pd.Series([100.0, 100.0, 100.0])
    assert count_vwap_crosses(close, vwap, lookback=2) == 1


def test_count_vwap_crosses_does_not_phantom_cross_on_repeated_nan():
    # Trailing NaN bars (e.g. truncated session) shouldn't conjure crosses.
    close = pd.Series([101.0, 102.0, 103.0])
    vwap = pd.Series([100.0, np.nan, np.nan])
    assert count_vwap_crosses(close, vwap, lookback=2) == 0


def test_time_filter_blocks_open():
    ts = datetime(2024, 1, 2, 9, 35, tzinfo=ZoneInfo("America/New_York"))
    allowed, reason = time_filter_allows(ts, avoid_open_minutes=10, avoid_close_minutes=60, scalp_mode=False)
    assert allowed is False
    assert "first" in reason


def test_time_filter_allows_midday():
    ts = datetime(2024, 1, 2, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    allowed, reason = time_filter_allows(ts, avoid_open_minutes=10, avoid_close_minutes=60, scalp_mode=False)
    assert allowed is True
    assert reason is None
