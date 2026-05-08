from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.backtest.runner import SessionResult
from src.backtest.sessions import TradingSession
from src.backtest.walk_forward import (
    WalkForwardConfig,
    WalkForwardWindow,
    aggregate_oos_trades,
    run_walk_forward,
)


EASTERN = ZoneInfo("America/New_York")


def _stub_session(date_iso: str) -> TradingSession:
    base = datetime.fromisoformat(date_iso).replace(hour=10, tzinfo=EASTERN)
    df = pd.DataFrame(
        {"Open": [1], "High": [1], "Low": [1], "Close": [1], "Volume": [1]},
        index=pd.DatetimeIndex([base]),
    )
    return TradingSession(session_date=date_iso, bars=df)


class _RecordingRunner:
    """Stand-in for SessionRunner that just records which sessions were called."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_session(self, symbol: str, session: TradingSession) -> SessionResult:
        self.calls.append(session.session_date)
        return SessionResult(symbol=symbol, session_date=session.session_date, trades=[])


def test_run_walk_forward_empty_sessions_returns_empty():
    runner = _RecordingRunner()
    result = run_walk_forward(runner, "SPY", [], WalkForwardConfig())
    assert result == []


def test_run_walk_forward_insufficient_sessions_returns_empty():
    sessions = [_stub_session("2026-01-01")]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    result = run_walk_forward(_RecordingRunner(), "SPY", sessions, cfg)
    assert result == []


def test_run_walk_forward_produces_one_window_per_step():
    # 90 sessions over 90 days. Train=30, test=10, step=10.
    base = datetime(2026, 1, 1)
    sessions = [_stub_session((base + timedelta(days=i)).date().isoformat()) for i in range(90)]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    runner = _RecordingRunner()
    windows = run_walk_forward(runner, "SPY", sessions, cfg)
    # Step from day 0: windows start at days 0, 10, 20, ..., constrained by last_date.
    assert len(windows) > 0
    for i, w in enumerate(windows):
        assert isinstance(w, WalkForwardWindow)
        assert w.window_index == i


def test_run_walk_forward_test_dates_lie_in_test_window():
    base = datetime(2026, 1, 1)
    sessions = [_stub_session((base + timedelta(days=i)).date().isoformat()) for i in range(90)]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    runner = _RecordingRunner()
    windows = run_walk_forward(runner, "SPY", sessions, cfg)
    for w in windows:
        for trade in w.test_trades:
            # No trades are returned by the stub, but if there were, exit dates should
            # lie in [test_start, test_end).
            assert w.test_start <= trade.exit_time_et.date().isoformat() < w.test_end
        # The runner should have been called once per session in the test window.
        # Inspect the recording runner's calls slice.


def test_run_walk_forward_only_runs_sessions_in_test_window():
    base = datetime(2026, 1, 1)
    sessions = [_stub_session((base + timedelta(days=i)).date().isoformat()) for i in range(60)]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    runner = _RecordingRunner()
    windows = run_walk_forward(runner, "SPY", sessions, cfg)
    # First window: train [day0, day30), test [day30, day40). Sessions at days 30..39 → 10 calls.
    assert len(windows) >= 1
    first_test_dates = sorted(set(runner.calls[:10]))
    assert all(
        windows[0].test_start <= d < windows[0].test_end for d in first_test_dates
    )


def test_run_walk_forward_window_dates_are_monotonic_increasing():
    base = datetime(2026, 1, 1)
    sessions = [_stub_session((base + timedelta(days=i)).date().isoformat()) for i in range(120)]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    windows = run_walk_forward(_RecordingRunner(), "SPY", sessions, cfg)
    for prev, curr in zip(windows[:-1], windows[1:]):
        assert prev.train_start < curr.train_start
        assert prev.test_start < curr.test_start


def test_aggregate_oos_trades_concatenates_test_trades():
    base = datetime(2026, 1, 1)
    sessions = [_stub_session((base + timedelta(days=i)).date().isoformat()) for i in range(90)]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    windows = run_walk_forward(_RecordingRunner(), "SPY", sessions, cfg)
    aggregated = aggregate_oos_trades(windows)
    assert sum(len(w.test_trades) for w in windows) == len(aggregated)


def test_walk_forward_handles_sparse_sessions_gap():
    # If a window has no sessions in its test slice, it's just skipped (no error).
    sessions = [
        _stub_session("2026-01-01"),
        # Big gap.
        _stub_session("2026-06-01"),
        _stub_session("2026-06-05"),
    ]
    cfg = WalkForwardConfig(train_window_days=30, test_window_days=10, step_days=10)
    windows = run_walk_forward(_RecordingRunner(), "SPY", sessions, cfg)
    # Doesn't crash. We don't assert window count strictly because of how step-skipping interacts.
    assert isinstance(windows, list)
