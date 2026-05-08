import pytest

from src.rate_limit import RateLimiter


def test_first_n_calls_admit_immediately():
    limiter = RateLimiter(calls_per_window=3, window_seconds=10.0)
    assert limiter.acquire(now=0.0) == 0.0
    assert limiter.acquire(now=0.1) == 0.0
    assert limiter.acquire(now=0.2) == 0.0


def test_throttles_when_window_full():
    limiter = RateLimiter(calls_per_window=2, window_seconds=10.0)
    limiter.acquire(now=0.0)
    limiter.acquire(now=1.0)
    wait = limiter.acquire(now=2.0)
    # The 3rd call has to wait until the 1st call ages out at t=10.
    assert wait == pytest.approx(8.001, abs=1e-3)


def test_old_calls_purged_from_window():
    limiter = RateLimiter(calls_per_window=2, window_seconds=5.0)
    limiter.acquire(now=0.0)
    limiter.acquire(now=1.0)
    # By t=10, both prior calls have aged out.
    assert limiter.acquire(now=10.0) == 0.0


def test_rejects_invalid_construction():
    with pytest.raises(ValueError):
        RateLimiter(calls_per_window=0)
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=0)
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=-1)


def test_continuous_steady_rate_does_not_throttle():
    limiter = RateLimiter(calls_per_window=5, window_seconds=60.0)
    waits = [limiter.acquire(now=float(i * 12.0)) for i in range(20)]
    # 5 per 60 seconds = one every 12s; all should clear without waiting.
    assert all(w == 0.0 for w in waits)
