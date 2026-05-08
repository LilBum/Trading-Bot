"""Driving loop: runs `LivePaperRunner` from session start to session end.

Calls `runner.run_iteration()` once per `tick_seconds`. Records each result
to the journal. Catches per-iteration exceptions so one bad tick doesn't
kill the whole session. Sleeps between ticks; clean exit at session_end.

Wraps the synchronous LivePaperRunner with the wall-clock cadence the live
shakedown needs. This is the thing a 5am scheduler would launch.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, time as _dtime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from src.futures_execution.journal import IterationJournal
from src.futures_execution.live_runner import LivePaperRunner


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DrivingLoopConfig:
    session_start_et: _dtime = _dtime(8, 0)
    session_end_et: _dtime = _dtime(16, 0)
    tick_seconds: int = 30        # how often to run an iteration
    pre_open_warmup_seconds: int = 0  # if > 0, allow this many seconds before session_start


def _et_now() -> datetime:
    return datetime.now(EASTERN)


class DrivingLoop:
    """Real-time driver around LivePaperRunner.

    For testing: inject `now_fn` (returns datetime) and `sleep_fn` (no-op).
    For production: defaults to wall-clock and `time.sleep`.
    """

    def __init__(
        self,
        runner: LivePaperRunner,
        journal: IterationJournal,
        config: DrivingLoopConfig,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.runner = runner
        self.journal = journal
        self.config = config
        self._now_fn = now_fn or _et_now
        self._sleep_fn = sleep_fn or _time.sleep

    def run(self) -> int:
        """Run from session_start to session_end. Returns the iteration count."""
        cfg = self.config
        iterations = 0

        while True:
            now = self._now_fn()
            if now.tzinfo is None:
                now = now.replace(tzinfo=EASTERN)

            session_start = now.replace(
                hour=cfg.session_start_et.hour,
                minute=cfg.session_start_et.minute,
                second=0,
                microsecond=0,
            )
            session_end = now.replace(
                hour=cfg.session_end_et.hour,
                minute=cfg.session_end_et.minute,
                second=0,
                microsecond=0,
            )

            if now < session_start:
                # Pre-session: sleep until session_start (or up to one tick).
                wait_seconds = (session_start - now).total_seconds()
                if cfg.pre_open_warmup_seconds and wait_seconds <= cfg.pre_open_warmup_seconds:
                    pass  # fall through to run_iteration
                else:
                    self._sleep_fn(min(wait_seconds, float(cfg.tick_seconds)))
                    continue

            if now >= session_end:
                break

            try:
                result = self.runner.run_iteration(now)
                self.journal.append(result)
            except Exception as exc:
                self.journal.append_error(now, f"runner exception: {exc}")
            iterations += 1
            self._sleep_fn(float(cfg.tick_seconds))

        return iterations
