from datetime import datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.futures_execution.ibkr_bars import EASTERN, IBKRBarsProvider


def _bar(year, month, day, hour, minute, *, o, h, l, c, v, tz=ZoneInfo("UTC")):
    """Build an ib_insync-shaped BarData stand-in."""
    return SimpleNamespace(
        date=datetime(year, month, day, hour, minute, tzinfo=tz),
        open=o, high=h, low=l, close=c, volume=v,
    )


def _ib_with_qualify(symbol="MNQ"):
    ib = MagicMock()
    contract = SimpleNamespace(symbol=symbol, exchange="CME", currency="USD")
    ib.qualifyContracts.return_value = [contract]
    return ib


# ----- contract resolution ----------------------------------------------


def test_provider_caches_qualified_contract():
    ib = _ib_with_qualify("MNQ")
    ib.reqHistoricalData.return_value = [
        _bar(2026, 5, 5, 12, 0, o=100, h=101, l=99, c=100, v=10),
    ]
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 8, 5, tzinfo=EASTERN),
    )
    p.get_session_bars("MNQ")
    p.get_session_bars("MNQ")
    assert ib.qualifyContracts.call_count == 1


def test_provider_raises_on_unknown_symbol():
    ib = _ib_with_qualify("ZZZ")
    p = IBKRBarsProvider(ib_client=ib)
    with pytest.raises(ValueError, match="Unknown"):
        p.get_session_bars("ZZZ")


def test_provider_raises_when_qualify_fails():
    ib = MagicMock()
    ib.qualifyContracts.return_value = []
    p = IBKRBarsProvider(ib_client=ib)
    with pytest.raises(RuntimeError, match="qualify"):
        p.get_session_bars("MNQ")


# ----- duration computation --------------------------------------------


def test_duration_pre_session_returns_token_duration():
    ib = _ib_with_qualify("MNQ")
    ib.reqHistoricalData.return_value = []
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 7, 0, tzinfo=EASTERN),  # before 8am session start
    )
    p.get_session_bars("MNQ")
    args, kwargs = ib.reqHistoricalData.call_args
    assert kwargs["durationStr"] == "60 S"


def test_duration_grows_through_session():
    ib = _ib_with_qualify("MNQ")
    ib.reqHistoricalData.return_value = []
    # 30 minutes into session
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN),
    )
    p.get_session_bars("MNQ")
    args, kwargs = ib.reqHistoricalData.call_args
    # Should be ~1860 seconds (30min * 60 + 60s pad)
    duration = kwargs["durationStr"]
    seconds = int(duration.split()[0])
    assert 1700 < seconds < 2000


def test_duration_clamped_to_24h():
    ib = _ib_with_qualify("MNQ")
    ib.reqHistoricalData.return_value = []
    # Hypothetically 25h after session start (shouldn't happen but defend)
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 6, 9, 0, tzinfo=EASTERN),
        session_start_et=time(8, 0),
    )
    p.get_session_bars("MNQ")
    args, kwargs = ib.reqHistoricalData.call_args
    seconds = int(kwargs["durationStr"].split()[0])
    assert seconds <= 86400


def test_request_uses_one_minute_bars_and_useRTH_false():
    ib = _ib_with_qualify("MNQ")
    ib.reqHistoricalData.return_value = []
    p = IBKRBarsProvider(ib_client=ib, now_fn=lambda: datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    p.get_session_bars("MNQ")
    args, kwargs = ib.reqHistoricalData.call_args
    assert kwargs["barSizeSetting"] == "1 min"
    assert kwargs["useRTH"] is False
    assert kwargs["whatToShow"] == "TRADES"


# ----- response handling -----------------------------------------------


def test_empty_response_returns_empty_dataframe():
    ib = _ib_with_qualify("MNQ")
    ib.reqHistoricalData.return_value = []
    p = IBKRBarsProvider(ib_client=ib, now_fn=lambda: datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    df = p.get_session_bars("MNQ")
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_bars_returned_as_et_indexed_ohlcv_dataframe():
    ib = _ib_with_qualify("MNQ")
    # IBKR returns UTC by default; check ET conversion.
    ib.reqHistoricalData.return_value = [
        _bar(2026, 5, 5, 13, 0, o=100, h=101, l=99, c=100.5, v=50),  # 09:00 ET
        _bar(2026, 5, 5, 13, 1, o=100.5, h=101.5, l=100, c=101, v=60),  # 09:01 ET
    ]
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 9, 5, tzinfo=EASTERN),
    )
    df = p.get_session_bars("MNQ")
    assert len(df) == 2
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert str(df.index.tz) == "America/New_York"
    # 13:00 UTC = 09:00 EDT
    assert df.index[0].hour == 9
    assert df.index[0].minute == 0
    # OHLCV preserved
    assert df.iloc[0]["Open"] == 100.0
    assert df.iloc[0]["Close"] == 100.5
    assert df.iloc[1]["Volume"] == 60.0


def test_bars_filtered_to_session_start():
    """Bars before session_start_et should be dropped from the response."""
    ib = _ib_with_qualify("MNQ")
    # Three bars: one pre-session, two post.
    ib.reqHistoricalData.return_value = [
        _bar(2026, 5, 5, 11, 0, o=100, h=101, l=99, c=100, v=10),  # 07:00 ET — pre-session
        _bar(2026, 5, 5, 12, 0, o=101, h=102, l=100, c=101, v=20),  # 08:00 ET — at session start
        _bar(2026, 5, 5, 12, 30, o=102, h=103, l=101, c=102, v=30),  # 08:30 ET — in session
    ]
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN),
    )
    df = p.get_session_bars("MNQ")
    assert len(df) == 2
    # First kept bar should be the 08:00 ET one
    assert df.index[0].hour == 8
    assert df.index[0].minute == 0


def test_bars_sorted_by_timestamp():
    ib = _ib_with_qualify("MNQ")
    # Inject out-of-order bars; provider should sort.
    ib.reqHistoricalData.return_value = [
        _bar(2026, 5, 5, 12, 30, o=2, h=2, l=2, c=2, v=20),
        _bar(2026, 5, 5, 12, 0, o=1, h=1, l=1, c=1, v=10),
    ]
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN),
    )
    df = p.get_session_bars("MNQ")
    assert df.index.is_monotonic_increasing


def test_volume_none_becomes_zero():
    ib = _ib_with_qualify("MNQ")
    bar_with_no_volume = SimpleNamespace(
        date=datetime(2026, 5, 5, 13, 0, tzinfo=ZoneInfo("UTC")),
        open=100, high=100, low=100, close=100, volume=None,
    )
    ib.reqHistoricalData.return_value = [bar_with_no_volume]
    p = IBKRBarsProvider(
        ib_client=ib,
        now_fn=lambda: datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN),
    )
    df = p.get_session_bars("MNQ")
    assert df.iloc[0]["Volume"] == 0.0
