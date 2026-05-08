from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd


EASTERN = ZoneInfo("America/New_York")


def count_vwap_crosses(close: pd.Series, vwap: pd.Series, lookback: int) -> int:
    if len(close) < lookback + 1:
        return 0
    recent_close = close.tail(lookback + 1)
    recent_vwap = vwap.tail(lookback + 1)
    deltas = recent_close - recent_vwap
    # NaN deltas (zero-volume bars where VWAP is undefined) get sign 0 and are
    # skipped. Don't fill numerically with 0.0 — that conflates "no signal" with
    # "exactly at VWAP" and the original cross loop also failed to carry the
    # last nonzero sign across NaN/zero gaps, missing real crosses.
    signs = pd.Series(0, index=deltas.index, dtype=int)
    signs.loc[deltas > 0] = 1
    signs.loc[deltas < 0] = -1
    crosses = 0
    prev_sign = 0
    for sign in signs:
        if sign == 0:
            continue
        if prev_sign != 0 and prev_sign != sign:
            crosses += 1
        prev_sign = sign
    return crosses


def time_filter_allows(
    timestamp: datetime,
    avoid_open_minutes: int,
    avoid_close_minutes: int,
    scalp_mode: bool,
) -> tuple[bool, str | None]:
    if scalp_mode:
        return True, None
    ts = timestamp.astimezone(EASTERN)
    market_open = datetime.combine(ts.date(), time(9, 30), tzinfo=EASTERN)
    market_close = datetime.combine(ts.date(), time(16, 0), tzinfo=EASTERN)
    minutes_since_open = (ts - market_open).total_seconds() / 60.0
    minutes_to_close = (market_close - ts).total_seconds() / 60.0
    if minutes_since_open < avoid_open_minutes:
        return False, f"Within first {avoid_open_minutes} minutes after open"
    if minutes_to_close < avoid_close_minutes:
        return False, f"Within last {avoid_close_minutes} minutes before close"
    return True, None
