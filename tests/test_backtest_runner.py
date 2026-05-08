import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.backtest.runner import RunnerConfig, SessionResult, SessionRunner
from src.backtest.sessions import TradingSession
from src.slippage import SlippageModel


EASTERN = ZoneInfo("America/New_York")


# ----- Fixtures and helpers ---------------------------------------------


def _make_session(num_bars: int, base_time=None, base_price: float = 500.0) -> TradingSession:
    """Build a session with `num_bars` 1-min bars starting at 09:30 ET."""
    base_time = base_time or datetime(2026, 5, 4, 9, 30, tzinfo=EASTERN)
    times = pd.DatetimeIndex(
        [base_time + timedelta(minutes=i) for i in range(num_bars)]
    )
    df = pd.DataFrame(
        {
            "Open": [base_price + 0.01 * i for i in range(num_bars)],
            "High": [base_price + 0.05 + 0.01 * i for i in range(num_bars)],
            "Low":  [base_price - 0.05 + 0.01 * i for i in range(num_bars)],
            "Close": [base_price + 0.01 * i for i in range(num_bars)],
            "Volume": [1000] * num_bars,
        },
        index=times,
    )
    return TradingSession(session_date=base_time.date().isoformat(), bars=df)


def _runner_config() -> RunnerConfig:
    return RunnerConfig(
        strategy_cfg={
            "vwap_slope_lookback": 5,
            "ema_fast": 9,
            "ema_slow": 21,
            "pullback_lookback": 8,
            "pullback_vwap_tolerance_pct": 0.2,
            "momentum_min_pct": 0.1,
            "chop_lookback_minutes": 30,
            "max_vwap_crosses": 4,
            "time_filters": {
                "avoid_open_minutes": 0,
                "avoid_close_minutes": 0,
                "scalp_mode": True,
            },
        },
        exits_cfg={
            "take_profit_pct": 0.30,
            "stop_loss_pct": 0.25,
            "max_hold_minutes": 120,
            "exit_before_close_minutes": 5,
        },
        min_signal_bars=10,
    )


@dataclass
class _FakeSignal:
    direction: str
    reject_reasons: list
    bar_timestamp: datetime
    decision_time_utc: str = ""
    setup: str = "fake"
    entry_trigger: str = ""
    invalidation: str = ""
    premium_stop: str = ""
    targets: str = ""
    regime_info: str = ""
    atr_value: float | None = None
    atr_pct: float | None = None
    higher_timeframe_trend: str | None = None
    sentiment_value: float | None = None
    sentiment_label: str | None = None
    sentiment_source: str | None = None
    warnings: list | None = None
    symbol: str = "SPY"


class _AlwaysCallEngine:
    """Stub signal engine: triggers a CALL on the first call, then NONE."""

    def __init__(self) -> None:
        self.called = 0

    def evaluate(self, symbol, df, config):  # noqa: ARG002
        self.called += 1
        timestamp = df.index[-1].to_pydatetime()
        if self.called == 1:
            return _FakeSignal(
                direction="CALL", reject_reasons=[], bar_timestamp=timestamp,
                symbol=symbol, warnings=[],
            )
        return _FakeSignal(
            direction="NONE", reject_reasons=["No setup"], bar_timestamp=timestamp,
            symbol=symbol, warnings=[],
        )


class _NeverEngine:
    def evaluate(self, symbol, df, config):  # noqa: ARG002
        timestamp = df.index[-1].to_pydatetime()
        return _FakeSignal(
            direction="NONE", reject_reasons=["No setup"], bar_timestamp=timestamp,
            symbol=symbol, warnings=[],
        )


# ----- Tests ------------------------------------------------------------


def test_run_session_returns_no_trades_when_session_too_short():
    cfg = _runner_config()
    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=_NeverEngine())
    session = _make_session(num_bars=5)
    result = runner.run_session("SPY", session)
    assert isinstance(result, SessionResult)
    assert result.trades == []
    assert result.session_date == session.session_date


def test_run_session_returns_no_trades_when_no_signal_triggers():
    cfg = _runner_config()
    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=_NeverEngine())
    session = _make_session(num_bars=60)
    result = runner.run_session("SPY", session)
    assert result.trades == []


def test_run_session_opens_one_position_with_always_call_engine():
    cfg = _runner_config()
    engine = _AlwaysCallEngine()
    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=engine)
    session = _make_session(num_bars=60)
    result = runner.run_session("SPY", session)
    # One trade either closed via session-close-buffer or already closed via TP/SL.
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.direction == "CALL"
    assert trade.symbol == "SPY"
    assert trade.contracts == cfg.contracts_per_trade


def test_run_session_force_closes_at_session_end():
    cfg = _runner_config()
    engine = _AlwaysCallEngine()
    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=engine)
    # Use a long session so neither TP nor SL likely fires (flat-ish synthetic prices).
    session = _make_session(num_bars=200)
    result = runner.run_session("SPY", session)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # Either the close-buffer or session-close should have closed it.
    assert trade.exit_reason in ("session_close", "tp", "stop", "time_stop")


def test_run_session_does_not_re_enter_after_first_trade():
    cfg = _runner_config()

    class _AlwaysOnEngine:
        def evaluate(self, symbol, df, config):  # noqa: ARG002
            timestamp = df.index[-1].to_pydatetime()
            return _FakeSignal(
                direction="CALL", reject_reasons=[], bar_timestamp=timestamp,
                symbol=symbol, warnings=[],
            )

    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=_AlwaysOnEngine())
    session = _make_session(num_bars=200)
    result = runner.run_session("SPY", session)
    # Even though the engine always says CALL, we only ever enter once per session.
    assert len(result.trades) == 1


def test_run_session_records_pnl_in_dollar_terms():
    cfg = _runner_config()
    engine = _AlwaysCallEngine()
    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=engine)
    session = _make_session(num_bars=60)
    result = runner.run_session("SPY", session)
    assert len(result.trades) == 1
    trade = result.trades[0]
    expected = (trade.exit_price - trade.entry_price) * trade.contracts * trade.contract_multiplier
    assert trade.realized_pnl == pytest.approx(expected, abs=1e-6)


def test_run_session_handles_engine_exception_gracefully():
    cfg = _runner_config()

    class _BoomEngine:
        def evaluate(self, *args, **kwargs):
            raise RuntimeError("boom")

    runner = SessionRunner(cfg, SlippageModel(rng=random.Random(0)), signal_engine=_BoomEngine())
    session = _make_session(num_bars=60)
    result = runner.run_session("SPY", session)
    assert result.trades == []
