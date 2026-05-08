import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from src.orb import compute_orb_info


def _make_df():
    eastern = ZoneInfo("America/New_York")
    times = pd.date_range("2024-01-02 09:30", periods=25, freq="1min", tz=eastern)
    data = []
    for idx, ts in enumerate(times):
        if idx < 15:
            high = 100.0
            low = 95.0
            close = 98.0
            vol = 100
        elif idx == 16:
            high = 101.0
            low = 99.5
            close = 101.0
            vol = 250
        else:
            high = 101.0
            low = 100.0
            close = 100.5
            vol = 120
        data.append({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol})
    df = pd.DataFrame(data, index=times)
    return df


def test_orb_breakout_retest_confirmed():
    df = _make_df()
    config = {
        "orb": {
            "enabled": True,
            "range_minutes": 15,
            "session_open_time": "09:30",
            "require_volume_confirmation": True,
            "volume_multiplier": 1.1,
            "volume_lookback": 5,
            "breakout_buffer_points": 0.0,
            "retest_band_points": 0.0,
            "hold_bars": 1,
            "require_retest_or_hold": True,
            "stop_loss_points": 12.0,
            "target_points": 40.0,
        }
    }
    info = compute_orb_info("GLD", df, config)
    assert info["status"] == "confirmed"
    assert info["direction"] == "CALL"
    assert info["entry"] == 100.0
    assert info["stop_loss"] == 88.0
    assert info["target"] == 140.0
