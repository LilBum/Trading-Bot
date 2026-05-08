from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.futures_backtest.sessions import (
    EASTERN,
    FuturesTradingSession,
    filter_session_window,
    load_bars_csv,
    load_sessions_for_symbol,
    split_into_sessions,
)


def _utc_iso(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).isoformat()


def _make_csv(tmp_path, rows, name="ES_1m.csv"):
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


# ----- load_bars_csv ----------------------------------------------------


def test_load_bars_csv_returns_et_indexed_ohlcv(tmp_path):
    path = _make_csv(
        tmp_path,
        [
            {
                "timestamp": _utc_iso(2026, 4, 1, 12, 0),  # 08:00 ET
                "Open": 5000.0, "High": 5001.0, "Low": 4999.0, "Close": 5000.5, "Volume": 100,
            },
        ],
    )
    df = load_bars_csv(path)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert str(df.index.tz) == "America/New_York"
    assert df.index[0].time() == time(8, 0)


def test_load_bars_csv_raises_on_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame([{"timestamp": _utc_iso(2026, 4, 1, 12, 0), "Open": 1}]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="missing columns"):
        load_bars_csv(path)


# ----- filter_session_window --------------------------------------------


def test_filter_session_window_excludes_overnight():
    overnight = datetime(2026, 5, 4, 3, 0, tzinfo=EASTERN)  # 03:00 ET
    morning = datetime(2026, 5, 4, 9, 0, tzinfo=EASTERN)    # 09:00 ET (in 8-16 window)
    afternoon = datetime(2026, 5, 4, 17, 0, tzinfo=EASTERN) # 17:00 ET (after window)
    df = pd.DataFrame(
        {"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3],
         "Close": [1, 2, 3], "Volume": [1, 2, 3]},
        index=pd.DatetimeIndex([overnight, morning, afternoon]),
    )
    out = filter_session_window(df)
    assert len(out) == 1
    assert out.index[0].hour == 9


def test_filter_session_window_excludes_weekends():
    saturday = datetime(2026, 5, 2, 10, 0, tzinfo=EASTERN)
    monday = datetime(2026, 5, 4, 10, 0, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=pd.DatetimeIndex([saturday, monday]),
    )
    out = filter_session_window(df)
    assert len(out) == 1
    assert out.index[0].weekday() == 0  # Monday


def test_filter_session_window_includes_8am_excludes_4pm():
    open_bar = datetime(2026, 5, 4, 8, 0, tzinfo=EASTERN)
    close_bar = datetime(2026, 5, 4, 16, 0, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=pd.DatetimeIndex([open_bar, close_bar]),
    )
    out = filter_session_window(df)
    assert len(out) == 1
    assert out.index[0].hour == 8


def test_filter_session_window_custom_times():
    early = datetime(2026, 5, 4, 7, 30, tzinfo=EASTERN)
    in_window = datetime(2026, 5, 4, 9, 30, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=pd.DatetimeIndex([early, in_window]),
    )
    out = filter_session_window(df, session_start=time(9, 0), session_end=time(15, 0))
    assert len(out) == 1
    assert out.index[0].hour == 9


# ----- split_into_sessions ----------------------------------------------


def test_split_into_sessions_one_per_day():
    bars = [
        (datetime(2026, 5, 4, 8, 0, tzinfo=EASTERN), 1),
        (datetime(2026, 5, 4, 8, 1, tzinfo=EASTERN), 2),
        (datetime(2026, 5, 5, 8, 0, tzinfo=EASTERN), 3),
    ]
    df = pd.DataFrame(
        {"Open": [b[1] for b in bars], "High": [b[1] for b in bars],
         "Low": [b[1] for b in bars], "Close": [b[1] for b in bars],
         "Volume": [10] * len(bars)},
        index=pd.DatetimeIndex([b[0] for b in bars]),
    )
    sessions = split_into_sessions(df, min_bars=1)
    assert len(sessions) == 2
    assert sessions[0].session_date == "2026-05-04"
    assert sessions[1].session_date == "2026-05-05"


def test_split_into_sessions_drops_short_days():
    # Only 5 bars on 2026-05-04, full 60+ bars on 2026-05-05.
    short_day = [datetime(2026, 5, 4, 8, m, tzinfo=EASTERN) for m in range(5)]
    full_day = [datetime(2026, 5, 5, 8, m, tzinfo=EASTERN) for m in range(60)] + \
               [datetime(2026, 5, 5, 9, m, tzinfo=EASTERN) for m in range(10)]
    times = short_day + full_day
    df = pd.DataFrame(
        {"Open": [1.0] * len(times), "High": [1.0] * len(times),
         "Low": [1.0] * len(times), "Close": [1.0] * len(times),
         "Volume": [10] * len(times)},
        index=pd.DatetimeIndex(times),
    )
    sessions = split_into_sessions(df, min_bars=60)
    # Short day (5 bars) dropped; full day kept.
    assert len(sessions) == 1
    assert sessions[0].session_date == "2026-05-05"


def test_split_into_sessions_empty_input():
    df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    assert split_into_sessions(df) == []


# ----- load_sessions_for_symbol -----------------------------------------


def test_load_sessions_for_symbol_round_trips(tmp_path):
    rows = [
        {
            "timestamp": _utc_iso(2026, 4, 1, 12, m),  # 08:0M ET
            "Open": 5000.0, "High": 5001.0, "Low": 4999.0, "Close": 5000.5, "Volume": 100,
        }
        for m in range(60)
    ]
    pd.DataFrame(rows).to_csv(tmp_path / "ES_1m.csv", index=False)
    sessions = load_sessions_for_symbol("ES", tmp_path, min_bars=10)
    assert len(sessions) == 1
    assert sessions[0].session_date == "2026-04-01"


def test_load_sessions_for_symbol_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="ES"):
        load_sessions_for_symbol("ES", tmp_path)


def test_futures_session_is_frozen():
    df = pd.DataFrame(
        {"Open": [1]},
        index=pd.DatetimeIndex([datetime(2026, 5, 4, 8, 0, tzinfo=EASTERN)]),
    )
    session = FuturesTradingSession(session_date="2026-05-04", bars=df)
    with pytest.raises(Exception):
        session.session_date = "2026-05-05"  # type: ignore[misc]
