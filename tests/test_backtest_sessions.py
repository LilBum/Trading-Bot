from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.backtest.sessions import (
    EASTERN,
    TradingSession,
    filter_rth,
    load_bars_csv,
    load_sessions_for_symbol,
    split_into_sessions,
)


def _make_csv(tmp_path, rows):
    path = tmp_path / "TST_1m.csv"
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return path


def _utc_iso(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).isoformat()


# ----- load_bars_csv ----------------------------------------------------


def test_load_bars_csv_returns_et_indexed_ohlcv(tmp_path):
    path = _make_csv(
        tmp_path,
        [
            {
                "timestamp": _utc_iso(2026, 4, 1, 13, 30),  # 09:30 ET
                "Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.05, "Volume": 100,
            },
            {
                "timestamp": _utc_iso(2026, 4, 1, 13, 31),  # 09:31 ET
                "Open": 1.05, "High": 1.15, "Low": 1.0, "Close": 1.10, "Volume": 120,
            },
        ],
    )
    df = load_bars_csv(path)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is not None
    assert str(df.index.tz) == "America/New_York"
    assert df.index[0].time() == time(9, 30)


def test_load_bars_csv_raises_on_missing_timestamp(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame([{"Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1}]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="timestamp"):
        load_bars_csv(path)


def test_load_bars_csv_raises_on_missing_ohlcv(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame([{"timestamp": _utc_iso(2026, 4, 1, 13, 30), "Open": 1}]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="missing columns"):
        load_bars_csv(path)


def test_load_bars_csv_drops_unparseable_timestamps(tmp_path):
    path = _make_csv(
        tmp_path,
        [
            {"timestamp": "not-a-date", "Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1},
            {
                "timestamp": _utc_iso(2026, 4, 1, 13, 30),
                "Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1,
            },
        ],
    )
    df = load_bars_csv(path)
    assert len(df) == 1


def test_load_bars_csv_sorts_by_timestamp(tmp_path):
    path = _make_csv(
        tmp_path,
        [
            {
                "timestamp": _utc_iso(2026, 4, 1, 13, 35),
                "Open": 2, "High": 2, "Low": 2, "Close": 2, "Volume": 200,
            },
            {
                "timestamp": _utc_iso(2026, 4, 1, 13, 30),
                "Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 100,
            },
        ],
    )
    df = load_bars_csv(path)
    assert df.index.is_monotonic_increasing
    assert df.iloc[0]["Open"] == 1


# ----- filter_rth -------------------------------------------------------


def test_filter_rth_excludes_weekends():
    # 2026-05-02 is a Saturday.
    saturday_idx = pd.DatetimeIndex(
        [datetime(2026, 5, 2, 10, 0, tzinfo=EASTERN)]
    )
    weekday_idx = pd.DatetimeIndex(
        [datetime(2026, 5, 1, 10, 0, tzinfo=EASTERN)]  # Friday
    )
    full_idx = saturday_idx.append(weekday_idx)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=full_idx,
    )
    out = filter_rth(df)
    assert len(out) == 1
    assert out.index[0].weekday() == 4  # Friday


def test_filter_rth_excludes_pre_market():
    pre = datetime(2026, 5, 1, 8, 0, tzinfo=EASTERN)  # 08:00 pre-market
    rth = datetime(2026, 5, 1, 10, 0, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=pd.DatetimeIndex([pre, rth]),
    )
    out = filter_rth(df)
    assert len(out) == 1
    assert out.index[0].hour == 10


def test_filter_rth_excludes_post_market():
    post = datetime(2026, 5, 1, 16, 30, tzinfo=EASTERN)
    rth = datetime(2026, 5, 1, 15, 30, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=pd.DatetimeIndex([post, rth]),
    )
    out = filter_rth(df)
    assert len(out) == 1
    assert out.index[0].hour == 15


def test_filter_rth_includes_open_bar_excludes_close_bar():
    # 09:30 should be included; 16:00 should not (last RTH bar is 15:59).
    open_bar = datetime(2026, 5, 1, 9, 30, tzinfo=EASTERN)
    close_bar = datetime(2026, 5, 1, 16, 0, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2]},
        index=pd.DatetimeIndex([open_bar, close_bar]),
    )
    out = filter_rth(df)
    assert len(out) == 1
    assert out.index[0].time() == time(9, 30)


def test_filter_rth_returns_empty_on_empty_input():
    df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    out = filter_rth(df)
    assert out.empty


# ----- split_into_sessions ----------------------------------------------


def test_split_into_sessions_returns_one_per_day():
    bars = [
        (datetime(2026, 5, 1, 10, 0, tzinfo=EASTERN), 1),
        (datetime(2026, 5, 1, 11, 0, tzinfo=EASTERN), 2),
        (datetime(2026, 5, 4, 10, 0, tzinfo=EASTERN), 3),
    ]
    df = pd.DataFrame(
        {"Open": [b[1] for b in bars], "High": [b[1] for b in bars],
         "Low": [b[1] for b in bars], "Close": [b[1] for b in bars],
         "Volume": [10] * len(bars)},
        index=pd.DatetimeIndex([b[0] for b in bars]),
    )
    sessions = split_into_sessions(df)
    assert len(sessions) == 2
    assert sessions[0].session_date == "2026-05-01"
    assert sessions[1].session_date == "2026-05-04"
    assert len(sessions[0].bars) == 2
    assert len(sessions[1].bars) == 1


def test_split_into_sessions_empty_input():
    df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    assert split_into_sessions(df) == []


def test_split_into_sessions_skips_weekend_bars():
    saturday = datetime(2026, 5, 2, 10, 0, tzinfo=EASTERN)
    monday = datetime(2026, 5, 4, 10, 0, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [10, 20]},
        index=pd.DatetimeIndex([saturday, monday]),
    )
    sessions = split_into_sessions(df)
    assert len(sessions) == 1
    assert sessions[0].session_date == "2026-05-04"


# ----- load_sessions_for_symbol -----------------------------------------


def test_load_sessions_for_symbol_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="TST"):
        load_sessions_for_symbol("TST", tmp_path)


def test_load_sessions_for_symbol_round_trips(tmp_path):
    rows = [
        {
            "timestamp": _utc_iso(2026, 4, 1, 13, 30),  # 09:30 ET
            "Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 10,
        },
        {
            "timestamp": _utc_iso(2026, 4, 1, 13, 31),
            "Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 10,
        },
        {
            "timestamp": _utc_iso(2026, 4, 2, 13, 30),
            "Open": 2, "High": 2, "Low": 2, "Close": 2, "Volume": 20,
        },
    ]
    path = tmp_path / "TST_1m.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    sessions = load_sessions_for_symbol("TST", tmp_path)
    assert len(sessions) == 2
    assert sessions[0].session_date == "2026-04-01"
    assert sessions[1].session_date == "2026-04-02"


# ----- TradingSession dataclass -----------------------------------------


def test_trading_session_is_frozen():
    df = pd.DataFrame({"Open": [1]}, index=pd.DatetimeIndex([datetime(2026, 5, 1, 10, 0, tzinfo=EASTERN)]))
    session = TradingSession(session_date="2026-05-01", bars=df)
    with pytest.raises(Exception):
        session.session_date = "2026-05-02"  # type: ignore[misc]
