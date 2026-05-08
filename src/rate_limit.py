"""Token-bucket-ish rate limiter for politely sharing free-tier API quotas."""

from __future__ import annotations

import time


class RateLimiter:
    """Allow at most `calls_per_window` calls per `window_seconds`.

    `acquire()` blocks (sleeps) when the quota is exhausted, returning the
    actual wait duration. Pass an explicit `now` to make the limiter
    deterministic in tests; in that mode it computes the wait but does not
    actually sleep.
    """

    def __init__(self, calls_per_window: int = 5, window_seconds: float = 60.0) -> None:
        if calls_per_window < 1:
            raise ValueError("calls_per_window must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.calls_per_window = calls_per_window
        self.window_seconds = float(window_seconds)
        self._history: list[float] = []

    def acquire(self, now: float | None = None) -> float:
        actual_now = now if now is not None else time.monotonic()
        self._purge_old(actual_now)
        wait = 0.0
        if len(self._history) >= self.calls_per_window:
            wait = self.window_seconds - (actual_now - self._history[0]) + 0.001
            wait = max(wait, 0.0)
            if now is None and wait > 0:
                time.sleep(wait)
                actual_now = time.monotonic()
                self._purge_old(actual_now)
        self._history.append(actual_now)
        return wait

    def _purge_old(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._history = [t for t in self._history if t > cutoff]
