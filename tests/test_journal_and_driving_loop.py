import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

from src.futures_execution.adapter import FuturesOrderAck
from src.futures_execution.driving_loop import DrivingLoop, DrivingLoopConfig
from src.futures_execution.journal import (
    IterationJournal,
    serialize_iteration_result,
)
from src.futures_execution.live_runner import IterationResult


EASTERN = ZoneInfo("America/New_York")


# ----- IterationJournal -------------------------------------------------


def _result(ts, action="no_signal", order_ack=None, note="") -> IterationResult:
    return IterationResult(
        timestamp_et=ts,
        bars_count=20,
        signal_direction=None,
        action=action,
        order_ack=order_ack,
        note=note,
    )


def test_journal_writes_one_jsonl_line_per_append(tmp_path):
    j = IterationJournal(tmp_path / "iter.jsonl")
    j.append(_result(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN)))
    j.append(_result(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN), action="hold"))
    lines = (tmp_path / "iter.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    record_0 = json.loads(lines[0])
    assert record_0["action"] == "no_signal"
    assert record_0["timestamp_et"] == "2026-05-05T08:30:00-04:00"


def test_journal_serializes_order_ack_dataclass(tmp_path):
    j = IterationJournal(tmp_path / "iter.jsonl")
    ack = FuturesOrderAck(status="filled", order_id="42", filled_qty=1, fill_price=4500.5)
    j.append(_result(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN), action="entry_filled", order_ack=ack))
    record = json.loads((tmp_path / "iter.jsonl").read_text(encoding="utf-8").strip())
    assert record["order_ack"]["status"] == "filled"
    assert record["order_ack"]["order_id"] == "42"
    assert record["order_ack"]["fill_price"] == 4500.5


def test_journal_handles_none_order_ack(tmp_path):
    j = IterationJournal(tmp_path / "iter.jsonl")
    j.append(_result(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN)))
    record = json.loads((tmp_path / "iter.jsonl").read_text(encoding="utf-8").strip())
    assert record["order_ack"] is None


def test_journal_append_error_records_exception(tmp_path):
    j = IterationJournal(tmp_path / "iter.jsonl")
    j.append_error(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN), "boom: signal engine died")
    record = json.loads((tmp_path / "iter.jsonl").read_text(encoding="utf-8").strip())
    assert record["action"] == "exception"
    assert "boom" in record["note"]


def test_journal_read_all_returns_records(tmp_path):
    j = IterationJournal(tmp_path / "iter.jsonl")
    j.append(_result(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN), action="entry_filled"))
    j.append(_result(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN), action="hold"))
    records = j.read_all()
    assert len(records) == 2
    assert records[0]["action"] == "entry_filled"
    assert records[1]["action"] == "hold"


def test_journal_read_all_empty_when_no_file(tmp_path):
    j = IterationJournal(tmp_path / "missing.jsonl")
    assert j.read_all() == []


def test_journal_creates_parent_directories(tmp_path):
    j = IterationJournal(tmp_path / "nested" / "subdir" / "iter.jsonl")
    j.append(_result(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN)))
    assert (tmp_path / "nested" / "subdir" / "iter.jsonl").exists()


def test_serialize_iteration_result_has_all_fields():
    ts = datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN)
    ack = FuturesOrderAck(status="filled", order_id="x", filled_qty=1, fill_price=100.0)
    result = IterationResult(
        timestamp_et=ts, bars_count=20, signal_direction="CALL",
        action="entry_filled", order_ack=ack, note="entry @ 100",
    )
    record = serialize_iteration_result(result)
    assert set(record.keys()) == {
        "timestamp_et", "bars_count", "signal_direction", "action", "order_ack", "note"
    }


# ----- DrivingLoop ------------------------------------------------------


@dataclass
class _StubRunner:
    """Stub runner that records each call and returns a canned result."""

    calls: list = field(default_factory=list)
    raise_on: Optional[int] = None
    raise_count: int = 0

    def run_iteration(self, now):
        self.calls.append(now)
        if self.raise_on is not None and len(self.calls) == self.raise_on:
            self.raise_count += 1
            raise RuntimeError(f"forced exception on call {self.raise_on}")
        return IterationResult(
            timestamp_et=now, bars_count=20, signal_direction=None,
            action="no_signal", note="stub",
        )


class _ClockTicker:
    """Returns a sequence of fixed datetimes from `now()` calls."""

    def __init__(self, times):
        self.times = list(times)
        self.idx = 0

    def now(self):
        if self.idx >= len(self.times):
            return self.times[-1]
        t = self.times[self.idx]
        self.idx += 1
        return t


def test_driving_loop_runs_for_session_window(tmp_path):
    journal = IterationJournal(tmp_path / "iter.jsonl")
    runner = _StubRunner()
    # Iterate at 8:00, 8:30, 9:00, 16:01 (which is past session_end).
    times = [
        datetime(2026, 5, 5, 8, 0, tzinfo=EASTERN),
        datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN),
        datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN),
        datetime(2026, 5, 5, 16, 1, tzinfo=EASTERN),
    ]
    clock = _ClockTicker(times)
    sleeps = []
    cfg = DrivingLoopConfig(tick_seconds=30)
    loop = DrivingLoop(runner, journal, cfg, now_fn=clock.now, sleep_fn=sleeps.append)
    iterations = loop.run()
    assert iterations == 3
    assert len(runner.calls) == 3
    assert len(journal.read_all()) == 3


def test_driving_loop_stops_at_session_end(tmp_path):
    journal = IterationJournal(tmp_path / "iter.jsonl")
    runner = _StubRunner()
    # One pre-session call (skipped), one post-end call (loop should break).
    clock = _ClockTicker([
        datetime(2026, 5, 5, 7, 30, tzinfo=EASTERN),  # pre-session
        datetime(2026, 5, 5, 16, 30, tzinfo=EASTERN),  # post-session
    ])
    cfg = DrivingLoopConfig(tick_seconds=30)
    loop = DrivingLoop(runner, journal, cfg, now_fn=clock.now, sleep_fn=lambda s: None)
    iterations = loop.run()
    # 0 iterations: pre-session sleeps once, then post-session loop breaks.
    assert iterations == 0
    assert runner.calls == []


def test_driving_loop_records_runner_exception_to_journal(tmp_path):
    journal = IterationJournal(tmp_path / "iter.jsonl")
    runner = _StubRunner(raise_on=2)  # second call raises
    times = [
        datetime(2026, 5, 5, 8, 0, tzinfo=EASTERN),
        datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN),  # raises
        datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN),
        datetime(2026, 5, 5, 16, 1, tzinfo=EASTERN),  # past end
    ]
    clock = _ClockTicker(times)
    cfg = DrivingLoopConfig(tick_seconds=30)
    loop = DrivingLoop(runner, journal, cfg, now_fn=clock.now, sleep_fn=lambda s: None)
    iterations = loop.run()
    records = journal.read_all()
    # 3 iterations attempted: 2 succeed, 1 raises. All 3 journaled.
    actions = [r["action"] for r in records]
    assert "exception" in actions
    assert iterations == 3


def test_driving_loop_continues_after_exception(tmp_path):
    """One exception shouldn't stop the loop — important for unattended runs."""
    journal = IterationJournal(tmp_path / "iter.jsonl")
    runner = _StubRunner(raise_on=1)
    times = [
        datetime(2026, 5, 5, 8, 0, tzinfo=EASTERN),
        datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN),
        datetime(2026, 5, 5, 16, 1, tzinfo=EASTERN),
    ]
    clock = _ClockTicker(times)
    cfg = DrivingLoopConfig(tick_seconds=30)
    loop = DrivingLoop(runner, journal, cfg, now_fn=clock.now, sleep_fn=lambda s: None)
    loop.run()
    records = journal.read_all()
    assert len(records) == 2  # one exception + one normal call
    actions = [r["action"] for r in records]
    assert "exception" in actions
    assert "no_signal" in actions
