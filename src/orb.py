from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd


EASTERN = ZoneInfo("America/New_York")


def compute_orb_info(symbol: str, df: pd.DataFrame, config: dict) -> Optional[dict]:
    orb_cfg = config.get("orb", {})
    if not orb_cfg.get("enabled", False):
        return None

    if df is None or df.empty:
        return {"status": "no_data"}

    range_minutes = int(orb_cfg.get("range_minutes", 15))
    open_time_str = orb_cfg.get("session_open_time", "09:30")
    volume_multiplier = float(orb_cfg.get("volume_multiplier", 1.2))
    volume_lookback = int(orb_cfg.get("volume_lookback", 20))
    breakout_buffer = float(orb_cfg.get("breakout_buffer_points", 0.0))
    retest_band = float(orb_cfg.get("retest_band_points", 0.0))
    hold_bars = int(orb_cfg.get("hold_bars", 1))
    require_volume = bool(orb_cfg.get("require_volume_confirmation", True))
    require_retest = bool(orb_cfg.get("require_retest_or_hold", True))
    stop_loss_points = float(orb_cfg.get("stop_loss_points", 12.0))
    target_points = float(orb_cfg.get("target_points", 40.0))

    index = df.index
    last_ts = index[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=EASTERN)
    else:
        last_ts = last_ts.tz_convert(EASTERN)
    session_date = last_ts.date()

    try:
        open_hour, open_minute = [int(val) for val in open_time_str.split(":")]
    except ValueError:
        open_hour, open_minute = 9, 30
    range_start = datetime.combine(session_date, time(open_hour, open_minute), tzinfo=EASTERN)
    range_end = range_start + timedelta(minutes=range_minutes)

    session_df = df.copy()
    if session_df.index.tz is None:
        session_df.index = session_df.index.tz_localize(EASTERN)
    else:
        session_df.index = session_df.index.tz_convert(EASTERN)

    range_df = session_df[(session_df.index >= range_start) & (session_df.index < range_end)]
    if range_df.empty:
        return {
            "status": "waiting_opening_range",
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
        }

    range_high = float(range_df["High"].max())
    range_low = float(range_df["Low"].min())

    after_range = session_df[session_df.index >= range_end]
    if after_range.empty:
        return {
            "status": "range_ready",
            "range_high": range_high,
            "range_low": range_low,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
        }

    avg_volume = session_df["Volume"].rolling(volume_lookback).mean()
    breakout = None
    for idx, row in after_range.iterrows():
        close_price = float(row["Close"])
        volume = float(row["Volume"])
        avg_vol = float(avg_volume.loc[idx]) if idx in avg_volume.index and pd.notna(avg_volume.loc[idx]) else None
        volume_ok = True
        if require_volume and avg_vol is not None:
            volume_ok = volume >= avg_vol * volume_multiplier
        elif require_volume and avg_vol is None:
            volume_ok = False

        if close_price > range_high + breakout_buffer and volume_ok:
            breakout = {
                "direction": "CALL",
                "time": idx,
                "price": close_price,
                "volume": volume,
                "avg_volume": avg_vol,
                "volume_ok": volume_ok,
            }
            break
        if close_price < range_low - breakout_buffer and volume_ok:
            breakout = {
                "direction": "PUT",
                "time": idx,
                "price": close_price,
                "volume": volume,
                "avg_volume": avg_vol,
                "volume_ok": volume_ok,
            }
            break

    if breakout is None:
        return {
            "status": "no_breakout",
            "range_high": range_high,
            "range_low": range_low,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
        }

    confirm_reason = None
    confirm_time = None
    direction = breakout["direction"]
    breakout_level = range_high if direction == "CALL" else range_low

    if not require_retest:
        confirm_reason = "breakout"
        confirm_time = breakout["time"]
        confirmed = True
    else:
        confirmed = False
    hold_count = 0
    for idx, row in after_range[after_range.index > breakout["time"]].iterrows():
        close_price = float(row["Close"])
        low_price = float(row["Low"])
        high_price = float(row["High"])
        if direction == "CALL":
            if low_price <= breakout_level + retest_band and close_price >= breakout_level:
                confirm_reason = "retest"
                confirm_time = idx
                break
            if close_price >= breakout_level:
                hold_count += 1
                if hold_count >= hold_bars:
                    confirm_reason = "hold"
                    confirm_time = idx
                    break
            else:
                hold_count = 0
        else:
            if high_price >= breakout_level - retest_band and close_price <= breakout_level:
                confirm_reason = "retest"
                confirm_time = idx
                break
            if close_price <= breakout_level:
                hold_count += 1
                if hold_count >= hold_bars:
                    confirm_reason = "hold"
                    confirm_time = idx
                    break
            else:
                hold_count = 0
    if confirm_reason is not None:
        confirmed = True
    status = "confirmed" if confirmed else "breakout_found"

    entry = breakout_level
    if direction == "CALL":
        stop = entry - stop_loss_points
        target = entry + target_points
    else:
        stop = entry + stop_loss_points
        target = entry - target_points

    return {
        "status": status,
        "direction": direction,
        "range_minutes": range_minutes,
        "range_high": range_high,
        "range_low": range_low,
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "breakout_time": breakout["time"].isoformat(),
        "breakout_price": breakout["price"],
        "breakout_volume": breakout["volume"],
        "avg_volume": breakout["avg_volume"],
        "volume_ok": breakout["volume_ok"],
        "confirm_reason": confirm_reason,
        "confirm_time": confirm_time.isoformat() if confirm_time is not None else None,
        "entry": entry,
        "stop_loss": stop,
        "target": target,
    }
