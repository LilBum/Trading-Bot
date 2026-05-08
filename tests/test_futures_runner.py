import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.futures_backtest.runner import (
    FuturesRunnerConfig,
    FuturesSessionResult,
    FuturesSessionRunner,
)
from src.futures_backtest.sessions import FuturesTradingSession
from src.futures_slippage import FuturesSlippageModel


EASTERN = ZoneInfo("America/New_York")


def _make_session(num_bars: int, base_time=None, base_price: float = 5000.0) -> FuturesTradingSession:
    base_time = base_time or datetime(2026, 5, 4, 8, 0, tzinfo=EASTERN)
    times = pd.DatetimeIndex(
        [base_time + timedelta(minutes=i) for i in range(num_bars)]
    )
    df = pd.DataFrame(
        {
            "Open":  [base_price + 0.25 * i for i in range(num_bars)],
            "High":  [base_price + 0.50 + 0.25 * i for i in range(num_bars)],
            "Low":   [base_price - 0.50 + 0.25 * i for i in range(num_bars)],
            "Close": [base_price + 0.25 * i for i in range(num_bars)],
            "Volume": [100] * num_bars,
        },
        index=times,
    )
    return FuturesTradingSession(session_date=base_time.date().isoformat(), bars=df)


def _runner_config(**overrides) -> FuturesRunnerConfig:
    base = dict(
        take_profit_points=8.0,
        stop_loss_points=4.0,
        max_hold_minutes=120,
        exit_before_close_minutes=5,
        contracts_per_trade=1,
        min_signal_bars=10,
    )
    base.update(overrides)
    return FuturesRunnerConfig(**base)


@dataclass
class _FakeSignal:
    direction: str
    reject_reasons: list = field(default_factory=list)
    bar_timestamp: datetime = field(default_factory=lambda: datetime.now(EASTERN))
    setup: str = "fake"
    regime_info: str = ""
    warnings: list = field(default_factory=list)


class _AlwaysCallEngine:
    """Stub engine: returns CALL the first time, NONE thereafter."""

    def __init__(self) -> None:
        self.called = 0

    def evaluate(self, symbol, df, config):  # noqa: ARG002
        self.called += 1
        ts = df.index[-1].to_pydatetime()
        if self.called == 1:
            return _FakeSignal(direction="CALL", bar_timestamp=ts)
        return _FakeSignal(direction="NONE", reject_reasons=["No setup"], bar_timestamp=ts)


class _AlwaysPutEngine:
    def __init__(self) -> None:
        self.called = 0

    def evaluate(self, symbol, df, config):  # noqa: ARG002
        self.called += 1
        ts = df.index[-1].to_pydatetime()
        if self.called == 1:
            return _FakeSignal(direction="PUT", bar_timestamp=ts)
        return _FakeSignal(direction="NONE", reject_reasons=["No setup"], bar_timestamp=ts)


class _NeverEngine:
    def evaluate(self, symbol, df, config):  # noqa: ARG002
        return _FakeSignal(direction="NONE", reject_reasons=["No setup"],
                           bar_timestamp=df.index[-1].to_pydatetime())


# ----- Tests ------------------------------------------------------------


def test_returns_no_trades_when_session_too_short():
    runner = FuturesSessionRunner(
        _runner_config(),
        FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=_NeverEngine(),
    )
    session = _make_session(num_bars=5)
    result = runner.run_session("ES", session)
    assert isinstance(result, FuturesSessionResult)
    assert result.trades == []


def test_returns_no_trades_when_no_signal_triggers():
    runner = FuturesSessionRunner(
        _runner_config(),
        FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=_NeverEngine(),
    )
    session = _make_session(num_bars=60)
    assert runner.run_session("ES", session).trades == []


def test_call_signal_opens_long_futures_position():
    engine = _AlwaysCallEngine()
    runner = FuturesSessionRunner(
        _runner_config(), FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=engine,
    )
    session = _make_session(num_bars=60)
    result = runner.run_session("ES", session)
    assert len(result.trades) == 1
    assert result.trades[0].side == "BUY"
    assert result.trades[0].symbol == "ES"


def test_put_signal_opens_short_futures_position():
    engine = _AlwaysPutEngine()
    runner = FuturesSessionRunner(
        _runner_config(), FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=engine,
    )
    session = _make_session(num_bars=60)
    result = runner.run_session("ES", session)
    assert len(result.trades) == 1
    assert result.trades[0].side == "SELL"


def test_only_one_position_per_session():
    class _AlwaysOnEngine:
        def evaluate(self, symbol, df, config):  # noqa: ARG002
            return _FakeSignal(direction="CALL", bar_timestamp=df.index[-1].to_pydatetime())

    runner = FuturesSessionRunner(
        _runner_config(), FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=_AlwaysOnEngine(),
    )
    session = _make_session(num_bars=200)
    result = runner.run_session("ES", session)
    assert len(result.trades) == 1


def test_force_closes_at_session_end_if_still_open():
    engine = _AlwaysCallEngine()
    # Tiny stops/targets that are far apart relative to the synthetic price drift,
    # so neither TP nor SL fires before session end.
    runner = FuturesSessionRunner(
        _runner_config(take_profit_points=10000.0, stop_loss_points=10000.0),
        FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=engine,
    )
    session = _make_session(num_bars=200)
    result = runner.run_session("ES", session)
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason in ("session_close", "time_stop")


def test_pnl_recorded_in_dollars_using_point_value():
    engine = _AlwaysCallEngine()
    runner = FuturesSessionRunner(
        _runner_config(),
        FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=engine,
    )
    session = _make_session(num_bars=60)
    result = runner.run_session("ES", session)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # ES point value is $50.
    expected = trade.realized_points * 50.0
    assert trade.realized_pnl == pytest.approx(expected, rel=1e-6)
    assert trade.point_value == pytest.approx(50.0)


def test_short_position_pnl_inverted_correctly():
    engine = _AlwaysPutEngine()
    # Use a price series that DRIFTS UP (adverse for shorts).
    runner = FuturesSessionRunner(
        _runner_config(stop_loss_points=10000.0, take_profit_points=10000.0),
        FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=engine,
    )
    session = _make_session(num_bars=60, base_price=5000.0)  # +0.25/bar drift
    result = runner.run_session("ES", session)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # Short opened near start, exited near end with price higher → loss.
    assert trade.side == "SELL"
    # Realized points should be negative because price drifted against short.
    assert trade.realized_points < 0


def test_runner_handles_engine_exception_gracefully():
    class _BoomEngine:
        def evaluate(self, *args, **kwargs):
            raise RuntimeError("boom")

    runner = FuturesSessionRunner(
        _runner_config(), FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=_BoomEngine(),
    )
    session = _make_session(num_bars=60)
    assert runner.run_session("ES", session).trades == []


def test_unknown_symbol_uses_fallback_contract_spec():
    """A symbol not in the contracts dict still runs (with fallback spec)."""
    engine = _AlwaysCallEngine()
    runner = FuturesSessionRunner(
        _runner_config(),
        FuturesSlippageModel(rng=random.Random(0)),
        signal_engine=engine,
    )
    session = _make_session(num_bars=60)
    result = runner.run_session("ZZ", session)
    # Should not crash; may or may not trade depending on fill.
    assert isinstance(result, FuturesSessionResult)
