from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.engines.orb_engine import OrbSignalEngine


EASTERN = ZoneInfo("America/New_York")


def _orb_cfg(**overrides):
    base = {
        "enabled": True,
        "range_minutes": 15,
        "session_open_time": "09:30",
        "require_volume_confirmation": False,
        "volume_multiplier": 1.0,
        "volume_lookback": 5,
        "breakout_buffer_points": 0.0,
        "retest_band_points": 0.0,
        "hold_bars": 1,
        "require_retest_or_hold": False,
        "stop_loss_points": 12.0,
        "target_points": 40.0,
    }
    base.update(overrides)
    return base


def _bars(n_bars: int, breakout_at: int | None = None, direction: str = "up") -> pd.DataFrame:
    """First `breakout_at` bars hold a tight range; later bars break that direction."""
    times = pd.date_range("2026-05-04 09:30", periods=n_bars, freq="1min", tz=EASTERN)
    rows = []
    for idx in range(n_bars):
        if breakout_at is None or idx < breakout_at:
            high, low, close = 100.0, 99.0, 99.5
        else:
            if direction == "up":
                high, low, close = 102.0, 100.5, 101.5
            else:
                high, low, close = 99.0, 97.0, 97.5
        rows.append({"Open": close, "High": high, "Low": low, "Close": close, "Volume": 1000})
    return pd.DataFrame(rows, index=times)


# ----- waiting / no-breakout cases --------------------------------------


def test_returns_none_during_opening_range():
    df = _bars(n_bars=10)  # only 10 bars; range is 15 min, not yet established
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "NONE"
    assert any("ORB status" in r for r in result.reject_reasons)


def test_returns_none_when_range_ready_but_no_post_range_bars():
    df = _bars(n_bars=15)  # exactly the opening range, nothing after
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "NONE"


def test_returns_none_when_no_breakout_after_range():
    df = _bars(n_bars=30, breakout_at=None)  # full session inside the range
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "NONE"


def test_returns_none_on_empty_dataframe():
    df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "NONE"
    assert "empty" in result.reject_reasons[0].lower()


# ----- confirmed breakout cases -----------------------------------------


def test_confirmed_call_breakout():
    df = _bars(n_bars=20, breakout_at=15, direction="up")
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "CALL"
    assert result.reject_reasons == []
    assert "Breakout" in result.setup
    assert "range" in result.regime_info.lower()


def test_confirmed_put_breakout():
    df = _bars(n_bars=20, breakout_at=15, direction="down")
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "PUT"
    assert result.reject_reasons == []


def test_force_enables_even_when_orb_cfg_disabled():
    cfg = _orb_cfg(enabled=False)  # adapter must override this
    df = _bars(n_bars=20, breakout_at=15, direction="up")
    engine = OrbSignalEngine(cfg)
    result = engine.evaluate("SPY", df, {})
    assert result.direction == "CALL"


# ----- runner-interface compatibility -----------------------------------


def test_signal_object_has_runner_required_fields():
    df = _bars(n_bars=20, breakout_at=15, direction="up")
    engine = OrbSignalEngine(_orb_cfg())
    result = engine.evaluate("SPY", df, {})
    # SessionRunner uses these directly:
    assert hasattr(result, "direction")
    assert hasattr(result, "reject_reasons")
    # ...and these for journaling/diagnostics:
    assert hasattr(result, "bar_timestamp")
    assert hasattr(result, "setup")
    assert hasattr(result, "regime_info")


def test_range_minutes_property_reads_from_config():
    engine = OrbSignalEngine(_orb_cfg(range_minutes=30))
    assert engine.range_minutes == 30


def test_evaluate_signal_can_be_consumed_by_session_runner_path():
    """Smoke test that a confirmed signal is shaped to feed _maybe_enter cleanly."""
    df = _bars(n_bars=20, breakout_at=15, direction="up")
    engine = OrbSignalEngine(_orb_cfg())
    signal = engine.evaluate("SPY", df, {})
    # Mirror the gate the runner uses:
    is_actionable = signal.direction in ("CALL", "PUT") and not signal.reject_reasons
    assert is_actionable
