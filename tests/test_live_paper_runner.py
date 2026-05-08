import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.futures_execution.adapter import FuturesOrderAck, FuturesQuote
from src.futures_execution.live_runner import (
    BarsProvider,
    IterationResult,
    LivePaperRunner,
    LivePaperRunnerConfig,
    LiveRunnerState,
    _Actions,
)
from src.futures_execution.paper import PaperFuturesExecutionAdapter
from src.futures_slippage import FuturesSlippageModel


EASTERN = ZoneInfo("America/New_York")


@dataclass
class _FakeBarsProvider:
    """Static set of bars. Tests overwrite via .bars."""

    bars: pd.DataFrame = field(default_factory=pd.DataFrame)

    def get_session_bars(self, symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return self.bars


@dataclass
class _StubQuoteProvider:
    quotes: dict[str, FuturesQuote] = field(default_factory=dict)

    def get_quote(self, symbol: str) -> FuturesQuote:
        return self.quotes[symbol]


@dataclass
class _FakeSignal:
    direction: str
    reject_reasons: list = field(default_factory=list)
    bar_timestamp: datetime = field(default_factory=lambda: datetime.now(EASTERN))
    setup: str = "fake"
    regime_info: str = ""
    warnings: list = field(default_factory=list)


class _AlwaysCallEngine:
    def __init__(self) -> None:
        self.called = 0

    def evaluate(self, symbol, df, config):  # noqa: ARG002
        self.called += 1
        ts = df.index[-1].to_pydatetime() if len(df) else datetime.now(EASTERN)
        if self.called == 1:
            return _FakeSignal(direction="CALL", bar_timestamp=ts)
        return _FakeSignal(direction="NONE", reject_reasons=["No setup"], bar_timestamp=ts)


class _AlwaysPutEngine:
    def __init__(self) -> None:
        self.called = 0

    def evaluate(self, symbol, df, config):  # noqa: ARG002
        self.called += 1
        ts = df.index[-1].to_pydatetime() if len(df) else datetime.now(EASTERN)
        if self.called == 1:
            return _FakeSignal(direction="PUT", bar_timestamp=ts)
        return _FakeSignal(direction="NONE", reject_reasons=["No setup"], bar_timestamp=ts)


class _NeverEngine:
    def evaluate(self, symbol, df, config):  # noqa: ARG002
        ts = df.index[-1].to_pydatetime() if len(df) else datetime.now(EASTERN)
        return _FakeSignal(direction="NONE", reject_reasons=["No setup"], bar_timestamp=ts)


class _BoomEngine:
    def evaluate(self, *args, **kwargs):
        raise RuntimeError("signal engine exploded")


# ----- helpers ----------------------------------------------------------


def _make_bars(n: int, base_price: float = 5000.0, base_time=None) -> pd.DataFrame:
    base_time = base_time or datetime(2026, 5, 5, 8, 0, tzinfo=EASTERN)
    times = pd.DatetimeIndex(
        [base_time + timedelta(minutes=i) for i in range(n)]
    )
    return pd.DataFrame(
        {
            "Open":  [base_price + 0.25 * i for i in range(n)],
            "High":  [base_price + 0.5 + 0.25 * i for i in range(n)],
            "Low":   [base_price - 0.5 + 0.25 * i for i in range(n)],
            "Close": [base_price + 0.25 * i for i in range(n)],
            "Volume": [100] * n,
        },
        index=times,
    )


def _paper_adapter(seed: int = 0, quote_provider=None):
    qp = quote_provider or _StubQuoteProvider(
        quotes={
            "ES": FuturesQuote(symbol="ES", bid=5000.00, ask=5000.25, quote_time_utc="t"),
            "NQ": FuturesQuote(symbol="NQ", bid=18000.00, ask=18000.25, quote_time_utc="t"),
            "MNQ": FuturesQuote(symbol="MNQ", bid=18000.00, ask=18000.25, quote_time_utc="t"),
        }
    )
    return PaperFuturesExecutionAdapter(
        slippage_model=FuturesSlippageModel(rng=random.Random(seed)),
        quote_provider=qp,
        et_time_fn=lambda: time(11, 0),
    )


def _runner(signal_engine=None, bars_provider=None, adapter=None, config_overrides=None):
    cfg = LivePaperRunnerConfig(
        take_profit_points=20.0,
        stop_loss_points=10.0,
        min_signal_bars=10,
    )
    if config_overrides:
        cfg = LivePaperRunnerConfig(**{**cfg.__dict__, **config_overrides})
    return LivePaperRunner(
        symbol="ES",
        signal_engine=signal_engine or _NeverEngine(),
        execution_adapter=adapter or _paper_adapter(),
        bars_provider=bars_provider or _FakeBarsProvider(_make_bars(20)),
        config=cfg,
    )


def _seed_open_position(
    adapter,
    symbol: str,
    side: str,
    entry_price: float,
    qty: int = 1,
    *,
    tp_price: float | None = None,
    sl_price: float | None = None,
    tp_points: float = 20.0,
    sl_points: float = 10.0,
    bracket_id: str = "test-bracket",
):
    """Wire up paper-adapter internal state as if a bracket had been submitted.

    Bypasses `submit_bracket` (which goes through the slippage model and
    yields a non-round entry price) so tests can pin entry price and
    TP/SL prices exactly. Use this for tests that exercise the runner's
    state transitions; tests that exercise `submit_bracket` itself live
    in test_futures_paper_adapter.py.
    """
    from src.futures_execution.paper import PaperPositionRecord
    from src.futures_slippage import CONTRACTS

    spec = CONTRACTS.get(symbol)
    point_value = spec.point_value if spec else 50.0
    adapter._positions[symbol] = PaperPositionRecord(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        entry_time_utc="seeded",
        point_value=point_value,
    )
    if tp_price is None:
        tp_price = entry_price + tp_points if side == "BUY" else entry_price - tp_points
    if sl_price is None:
        sl_price = entry_price - sl_points if side == "BUY" else entry_price + sl_points
    adapter._active_brackets[symbol] = {
        "bracket_id": bracket_id,
        "tp_price": float(tp_price),
        "sl_price": float(sl_price),
        "tp_order_id": f"{bracket_id}-tp",
        "sl_order_id": f"{bracket_id}-sl",
        "side": side,
        "qty": qty,
    }


# ----- warmup -----------------------------------------------------------


def test_warmup_returns_when_too_few_bars():
    bp = _FakeBarsProvider(_make_bars(5))  # below the min_signal_bars=10 default
    r = _runner(bars_provider=bp)
    result = r.run_iteration(datetime(2026, 5, 5, 8, 5, tzinfo=EASTERN))
    assert result.action == _Actions.WARMUP
    assert "need 10 bars" in result.note


# ----- no signal --------------------------------------------------------


def test_never_engine_returns_no_signal():
    r = _runner(signal_engine=_NeverEngine())
    result = r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert result.action == _Actions.NO_SIGNAL
    assert result.signal_direction == "NONE"
    assert r.state.open_position_side is None


def test_signal_engine_exception_returns_signal_error():
    r = _runner(signal_engine=_BoomEngine())
    result = r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert result.action == _Actions.SIGNAL_ERROR
    assert "exploded" in result.note
    assert r.state.open_position_side is None


# ----- entry ------------------------------------------------------------


def test_call_signal_opens_long_position():
    engine = _AlwaysCallEngine()
    r = _runner(signal_engine=engine)
    result = r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert result.action == _Actions.ENTRY_FILLED
    assert result.signal_direction == "CALL"
    assert r.state.open_position_side == "BUY"
    assert r.state.open_position_qty == 1
    assert r.state.entered_today is True


def test_put_signal_opens_short_position():
    engine = _AlwaysPutEngine()
    r = _runner(signal_engine=engine)
    result = r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert result.action == _Actions.ENTRY_FILLED
    assert r.state.open_position_side == "SELL"


def test_only_one_entry_per_day_even_when_signal_keeps_firing():
    class _AlwaysCall:
        def evaluate(self, symbol, df, config):  # noqa: ARG002
            ts = df.index[-1].to_pydatetime()
            return _FakeSignal(direction="CALL", bar_timestamp=ts)

    r = _runner(signal_engine=_AlwaysCall())
    r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))  # entry
    # Force exit so position is flat but entered_today stays True
    r._clear_position()
    result = r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))
    assert result.action == _Actions.HOLD
    assert "already entered" in result.note


# ----- hold + exit triggers ---------------------------------------------


def test_hold_when_position_open_and_no_exit_trigger():
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    bp = _FakeBarsProvider(_make_bars(20, base_price=5000.0))  # current ~5004.75
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "BUY", entry_price=5000.0)
    r = _runner(bars_provider=bp, adapter=adapter)
    r.state = state
    result = r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))
    assert result.action == _Actions.HOLD
    assert r.state.open_position_side == "BUY"


def test_take_profit_triggers_exit():
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    # Bars where last close = 5025 = +25pt. TP threshold is 20pt → fires.
    bars = _make_bars(20, base_price=5000.0)
    bars.loc[bars.index[-1], "Close"] = 5025.0
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "BUY", entry_price=5000.0)
    r = _runner(bars_provider=_FakeBarsProvider(bars), adapter=adapter)
    r.state = state
    result = r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))
    assert result.action == _Actions.EXIT_TP
    assert r.state.open_position_side is None


def test_stop_loss_triggers_exit():
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    bars = _make_bars(20, base_price=5000.0)
    bars.loc[bars.index[-1], "Close"] = 4985.0  # -15pt → triggers stop at 10pt
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "BUY", entry_price=5000.0)
    r = _runner(bars_provider=_FakeBarsProvider(bars), adapter=adapter)
    r.state = state
    result = r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))
    assert result.action == _Actions.EXIT_STOP
    assert r.state.open_position_side is None


def test_short_take_profit_triggers_when_price_falls():
    state = LiveRunnerState(
        open_position_side="SELL",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    bars = _make_bars(20, base_price=5000.0)
    bars.loc[bars.index[-1], "Close"] = 4975.0  # -25pt move = +25pt for short
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "SELL", entry_price=5000.0)
    r = _runner(bars_provider=_FakeBarsProvider(bars), adapter=adapter)
    r.state = state
    result = r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))
    assert result.action == _Actions.EXIT_TP


def test_short_stop_loss_triggers_when_price_rises():
    state = LiveRunnerState(
        open_position_side="SELL",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    bars = _make_bars(20, base_price=5000.0)
    bars.loc[bars.index[-1], "Close"] = 5015.0
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "SELL", entry_price=5000.0)
    r = _runner(bars_provider=_FakeBarsProvider(bars), adapter=adapter)
    r.state = state
    result = r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))
    assert result.action == _Actions.EXIT_STOP


def test_session_close_triggers_when_within_buffer():
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    bars = _make_bars(20, base_price=5000.0)
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "BUY", entry_price=5000.0)
    r = _runner(bars_provider=_FakeBarsProvider(bars), adapter=adapter)
    r.state = state
    # 15:57 ET = 3 minutes to close, default buffer is 5
    result = r.run_iteration(datetime(2026, 5, 5, 15, 57, tzinfo=EASTERN))
    assert result.action == _Actions.EXIT_SESSION_CLOSE
    assert r.state.open_position_side is None


def test_session_close_does_not_trigger_outside_buffer():
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    bars = _make_bars(20, base_price=5000.0)
    adapter = _paper_adapter()
    _seed_open_position(adapter, "ES", "BUY", entry_price=5000.0)
    r = _runner(bars_provider=_FakeBarsProvider(bars), adapter=adapter)
    r.state = state
    # 15:30 ET = 30 minutes to close, well outside the 5min buffer
    result = r.run_iteration(datetime(2026, 5, 5, 15, 30, tzinfo=EASTERN))
    assert result.action == _Actions.HOLD


# ----- state transitions across iterations ------------------------------


def test_state_persists_between_iterations():
    engine = _AlwaysCallEngine()
    r = _runner(signal_engine=engine)
    r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert r.state.open_position_side == "BUY"
    r.run_iteration(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN))
    # Position should still be open; second iteration sees position and holds.
    assert r.state.open_position_side == "BUY"


def test_after_exit_no_re_entry_in_same_session():
    engine = _AlwaysCallEngine()
    r = _runner(signal_engine=engine)
    r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))  # entry
    # Force a TP exit by raising close above the entry by enough.
    state_after_entry = r.state
    bars = _make_bars(20, base_price=5000.0)
    bars.loc[bars.index[-1], "Close"] = state_after_entry.open_position_entry_price + 25
    r.bars_provider = _FakeBarsProvider(bars)
    r.run_iteration(datetime(2026, 5, 5, 9, 0, tzinfo=EASTERN))  # exit
    assert r.state.open_position_side is None
    # Third iteration: position is flat, but entered_today=True, so HOLD.
    result = r.run_iteration(datetime(2026, 5, 5, 9, 5, tzinfo=EASTERN))
    assert result.action == _Actions.HOLD


# ----- reconcile-on-start ------------------------------------------------


def test_recover_state_finds_existing_position_at_broker():
    adapter = _paper_adapter()
    # Pre-existing open position at broker — e.g. from a previous process
    # that crashed mid-trade.
    _seed_open_position(adapter, "ES", "BUY", entry_price=5000.0, qty=2)
    r = _runner(adapter=adapter)
    # State starts empty.
    assert r.state.open_position_side is None
    recovered = r.recover_state_from_broker()
    assert recovered is not None
    assert recovered.state == "open"
    assert r.state.open_position_side == "BUY"
    assert r.state.open_position_qty == 2
    assert r.state.open_position_entry_price == 5000.0
    assert r.state.entered_today is True


def test_recover_state_no_op_when_broker_is_flat():
    adapter = _paper_adapter()
    r = _runner(adapter=adapter)
    recovered = r.recover_state_from_broker()
    assert recovered is None
    assert r.state.open_position_side is None
    assert r.state.entered_today is False


def test_recover_state_swallows_adapter_exception():
    """Broker query failures shouldn't crash startup — log and proceed flat."""
    class _BoomAdapter:
        def poll_position(self, symbol, *, reference_price=None):
            raise RuntimeError("ib disconnected")
        def submit_order(self, *a, **kw): pass
        def cancel_order(self, *a, **kw): pass
        def get_open_positions(self): return []
        def reconcile(self): return {}
        def submit_bracket(self, *a, **kw): pass
        def flatten_position(self, *a, **kw): pass

    r = _runner(adapter=_BoomAdapter())
    recovered = r.recover_state_from_broker()
    assert recovered is None
    assert r.state.open_position_side is None


# ----- signal vs execution symbol routing --------------------------------


def test_runner_routes_signal_and_execution_to_separate_symbols():
    """NQ→MNQ: signal generated from NQ bars, orders routed to MNQ."""
    nq_bars = _make_bars(20, base_price=18000.0)

    class _SymbolTrackingBars:
        def __init__(self):
            self.requested = []
        def get_session_bars(self, symbol):
            self.requested.append(symbol)
            return nq_bars

    bars_tracker = _SymbolTrackingBars()
    quotes = _StubQuoteProvider(quotes={
        "NQ":  FuturesQuote(symbol="NQ",  bid=18000.00, ask=18000.25, quote_time_utc="t"),
        "MNQ": FuturesQuote(symbol="MNQ", bid=18000.00, ask=18000.25, quote_time_utc="t"),
    })
    adapter = PaperFuturesExecutionAdapter(
        slippage_model=FuturesSlippageModel(rng=random.Random(0)),
        quote_provider=quotes,
        et_time_fn=lambda: time(11, 0),
    )

    cfg = LivePaperRunnerConfig(
        take_profit_points=100.0, stop_loss_points=50.0, min_signal_bars=10,
    )
    r = LivePaperRunner(
        signal_engine=_AlwaysCallEngine(),
        execution_adapter=adapter,
        bars_provider=bars_tracker,
        config=cfg,
        signal_symbol="NQ",
        execution_symbol="MNQ",
    )
    assert r.signal_symbol == "NQ"
    assert r.execution_symbol == "MNQ"

    result = r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert result.action == _Actions.ENTRY_FILLED
    # Bars were requested with the SIGNAL symbol.
    assert "NQ" in bars_tracker.requested
    # Order routed to MNQ — the paper adapter should have an MNQ position now.
    assert "MNQ" in adapter._positions
    assert "NQ" not in adapter._positions


def test_runner_back_compat_symbol_argument_sets_both_sides():
    """Pre-split callers using `symbol=` should still work; both sides default to it."""
    r = _runner()  # uses symbol="ES"
    assert r.signal_symbol == "ES"
    assert r.execution_symbol == "ES"
    assert r.symbol == "ES"


def test_runner_rejects_partial_symbol_specification():
    """Either provide `symbol=`, or both `signal_symbol=` AND `execution_symbol=`."""
    with pytest.raises(ValueError, match="provide both"):
        LivePaperRunner(
            signal_engine=_NeverEngine(),
            execution_adapter=_paper_adapter(),
            bars_provider=_FakeBarsProvider(_make_bars(20)),
            config=LivePaperRunnerConfig(),
            signal_symbol="NQ",
            # execution_symbol intentionally missing
        )


def test_runner_rejects_no_symbol_specification():
    with pytest.raises(ValueError, match="must provide"):
        LivePaperRunner(
            signal_engine=_NeverEngine(),
            execution_adapter=_paper_adapter(),
            bars_provider=_FakeBarsProvider(_make_bars(20)),
            config=LivePaperRunnerConfig(),
        )


# ----- pending-entry resolution (broker-side ack delay) ------------------


class _SubmittedThenFilledAdapter:
    """Adapter that returns Submitted on first submit_bracket, then Open
    on subsequent poll_position calls — simulating IBKR returning before
    the fill ack lands.
    """

    def __init__(self):
        self.fill_price = 5001.0
        self._poll_count = 0
        self._fill_after_n_polls = 1

    def submit_bracket(self, intent):
        from src.futures_execution.adapter import BracketAck, FuturesOrderAck
        return BracketAck(
            status="active",
            entry_ack=FuturesOrderAck(
                status="submitted", order_id="parent-101",
                submission_time_utc="t",
            ),
            take_profit_order_id="parent-102",
            stop_loss_order_id="parent-103",
            bracket_id=intent.bracket_id,
        )

    def poll_position(self, symbol, *, reference_price=None):
        from src.futures_execution.adapter import PositionStatus
        self._poll_count += 1
        if self._poll_count > self._fill_after_n_polls:
            return PositionStatus(
                state="open", side="BUY", qty=1,
                entry_price=self.fill_price,
            )
        return PositionStatus(state="flat", note="parent still pending")

    def submit_order(self, intent):
        from src.futures_execution.adapter import FuturesOrderAck
        return FuturesOrderAck(status="rejected", reason="not used")
    def cancel_order(self, *a, **kw): pass
    def get_open_positions(self): return []
    def reconcile(self): return {}
    def flatten_position(self, *a, **kw):
        from src.futures_execution.adapter import FuturesOrderAck
        return FuturesOrderAck(status="rejected", reason="not used")


def test_pending_entry_holds_until_parent_fills():
    """Submit returns Submitted; first poll returns flat -> PENDING_ENTRY_HOLD."""
    engine = _AlwaysCallEngine()
    adapter = _SubmittedThenFilledAdapter()
    cfg = LivePaperRunnerConfig(min_signal_bars=10)
    r = LivePaperRunner(
        symbol="ES",
        signal_engine=engine,
        execution_adapter=adapter,
        bars_provider=_FakeBarsProvider(_make_bars(20)),
        config=cfg,
    )
    # First iteration: signal fires, bracket submitted but parent pending.
    r1 = r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert r1.action == _Actions.ENTRY_SUBMITTED
    assert r.state.active_bracket_id is not None
    assert r.state.entered_today is True
    assert r.state.open_position_side is None  # not yet filled

    # Second iteration: poll says still flat. Should HOLD as pending.
    r2 = r.run_iteration(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN))
    assert r2.action == _Actions.PENDING_ENTRY_HOLD
    assert r.state.open_position_side is None


def test_pending_entry_promotes_to_filled_when_broker_reports_open():
    """Submit returns Submitted; second poll returns open -> ENTRY_FILLED."""
    engine = _AlwaysCallEngine()
    adapter = _SubmittedThenFilledAdapter()
    cfg = LivePaperRunnerConfig(min_signal_bars=10)
    r = LivePaperRunner(
        symbol="ES",
        signal_engine=engine,
        execution_adapter=adapter,
        bars_provider=_FakeBarsProvider(_make_bars(20)),
        config=cfg,
    )
    r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))  # submitted
    r.run_iteration(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN))  # still pending
    r3 = r.run_iteration(datetime(2026, 5, 5, 8, 32, tzinfo=EASTERN))
    assert r3.action == _Actions.ENTRY_FILLED
    assert r.state.open_position_side == "BUY"
    assert r.state.open_position_qty == 1
    assert r.state.open_position_entry_price == 5001.0


class _UnknownThenOpenAdapter:
    """Adapter that returns 'unknown' on first poll then 'open' on subsequent
    polls — simulating a transient broker query failure that recovers."""

    def __init__(self, side="BUY", entry_price=5000.0, qty=1):
        self._poll_count = 0
        self._side = side
        self._entry_price = entry_price
        self._qty = qty

    def poll_position(self, symbol, *, reference_price=None):
        from src.futures_execution.adapter import PositionStatus
        self._poll_count += 1
        if self._poll_count == 1:
            return PositionStatus(
                state="unknown", note="positions query failed: timeout",
            )
        return PositionStatus(
            state="open", side=self._side, qty=self._qty,
            entry_price=self._entry_price,
        )

    def submit_bracket(self, *a, **kw): pass
    def submit_order(self, *a, **kw): pass
    def cancel_order(self, *a, **kw): pass
    def get_open_positions(self): return []
    def reconcile(self): return {}
    def flatten_position(self, *a, **kw): pass


def test_unknown_state_during_open_position_preserves_runner_state():
    """A transient positions() failure must NOT cause the runner to drop
    tracking of a real open position."""
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
        active_bracket_id="b1",
    )
    adapter = _UnknownThenOpenAdapter()
    cfg = LivePaperRunnerConfig(min_signal_bars=10)
    r = LivePaperRunner(
        symbol="ES",
        signal_engine=_NeverEngine(),
        execution_adapter=adapter,
        bars_provider=_FakeBarsProvider(_make_bars(20)),
        config=cfg,
        state=state,
    )
    # First iteration: adapter returns unknown. Runner must HOLD without
    # clearing state.
    r1 = r.run_iteration(datetime(2026, 5, 5, 10, 0, tzinfo=EASTERN))
    assert r1.action == _Actions.HOLD
    assert "unknown" in r1.note.lower()
    assert r.state.open_position_side == "BUY"
    assert r.state.open_position_entry_price == 5000.0
    assert r.state.active_bracket_id == "b1"

    # Second iteration: adapter recovers and reports open. Runner sees
    # open and continues to manage the position normally.
    r2 = r.run_iteration(datetime(2026, 5, 5, 10, 1, tzinfo=EASTERN))
    assert r2.action == _Actions.HOLD  # bracket active, no exit, no session close yet
    assert r.state.open_position_side == "BUY"


class _UnknownPendingAdapter:
    """Returns ENTRY_SUBMITTED, then 'unknown', then 'open' — simulates a
    broker disconnect during the pending-entry resolve window."""

    def __init__(self):
        self._poll_count = 0

    def submit_bracket(self, intent):
        from src.futures_execution.adapter import BracketAck, FuturesOrderAck
        return BracketAck(
            status="active",
            entry_ack=FuturesOrderAck(
                status="submitted", order_id="parent-401",
                submission_time_utc="t",
            ),
            take_profit_order_id="402",
            stop_loss_order_id="403",
            bracket_id=intent.bracket_id,
        )

    def poll_position(self, symbol, *, reference_price=None):
        from src.futures_execution.adapter import PositionStatus
        self._poll_count += 1
        if self._poll_count == 1:
            return PositionStatus(state="unknown", note="broker disconnect")
        return PositionStatus(state="open", side="BUY", qty=1, entry_price=5001.0)

    def submit_order(self, *a, **kw): pass
    def cancel_order(self, *a, **kw): pass
    def get_open_positions(self): return []
    def reconcile(self): return {}
    def flatten_position(self, *a, **kw): pass


def test_unknown_state_during_pending_entry_preserves_bracket_tracking():
    """If the broker is unreachable while we're waiting on a parent fill,
    keep the pending bracket state intact and retry."""
    engine = _AlwaysCallEngine()
    adapter = _UnknownPendingAdapter()
    cfg = LivePaperRunnerConfig(min_signal_bars=10)
    r = LivePaperRunner(
        symbol="ES",
        signal_engine=engine,
        execution_adapter=adapter,
        bars_provider=_FakeBarsProvider(_make_bars(20)),
        config=cfg,
    )
    # Iteration 1: submit bracket → submitted
    r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))
    assert r.state.active_bracket_id is not None
    assert r.state.open_position_side is None

    # Iteration 2: broker query fails. Stay in pending, don't lose bracket.
    r2 = r.run_iteration(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN))
    assert r2.action == _Actions.PENDING_ENTRY_HOLD
    assert "unknown" in r2.note.lower()
    assert r.state.active_bracket_id is not None  # bracket NOT dropped
    assert r.state.open_position_side is None     # still pending

    # Iteration 3: broker recovers. Promote to filled.
    r3 = r.run_iteration(datetime(2026, 5, 5, 8, 32, tzinfo=EASTERN))
    assert r3.action == _Actions.ENTRY_FILLED
    assert r.state.open_position_side == "BUY"


class _FlattenRejectingAdapter:
    """Adapter that reports 'open' on poll but rejects flatten_position.
    Simulates an outage where positions query works but order placement
    is failing."""

    def poll_position(self, symbol, *, reference_price=None):
        from src.futures_execution.adapter import PositionStatus
        return PositionStatus(state="open", side="BUY", qty=1, entry_price=5000.0)

    def flatten_position(self, *a, **kw):
        from src.futures_execution.adapter import FuturesOrderAck
        return FuturesOrderAck(
            status="rejected", reason="broker rejected: connection error",
        )

    def submit_bracket(self, *a, **kw): pass
    def submit_order(self, *a, **kw): pass
    def cancel_order(self, *a, **kw): pass
    def get_open_positions(self): return []
    def reconcile(self): return {}


def test_flatten_rejection_at_session_close_preserves_state():
    """If flatten fails, do NOT clear local state — let next iteration retry."""
    state = LiveRunnerState(
        open_position_side="BUY",
        open_position_qty=1,
        open_position_entry_price=5000.0,
        entered_today=True,
    )
    cfg = LivePaperRunnerConfig(min_signal_bars=10)
    r = LivePaperRunner(
        symbol="ES",
        signal_engine=_NeverEngine(),
        execution_adapter=_FlattenRejectingAdapter(),
        bars_provider=_FakeBarsProvider(_make_bars(20)),
        config=cfg,
        state=state,
    )
    # 15:57 ET = inside session-close buffer. Runner attempts flatten.
    result = r.run_iteration(datetime(2026, 5, 5, 15, 57, tzinfo=EASTERN))
    assert result.action == _Actions.EXIT_REJECTED
    assert "retry" in result.note.lower()
    # Critical: state must NOT be cleared so next iteration retries.
    assert r.state.open_position_side == "BUY"
    assert r.state.open_position_qty == 1


def test_submitted_state_blocks_re_entry_attempt():
    """A second iteration with active_bracket_id set must NOT call signal engine."""
    engine = _AlwaysCallEngine()  # would fire CALL again if reached
    adapter = _SubmittedThenFilledAdapter()
    adapter._fill_after_n_polls = 1000  # never fills
    cfg = LivePaperRunnerConfig(min_signal_bars=10)
    r = LivePaperRunner(
        symbol="ES",
        signal_engine=engine,
        execution_adapter=adapter,
        bars_provider=_FakeBarsProvider(_make_bars(20)),
        config=cfg,
    )
    r.run_iteration(datetime(2026, 5, 5, 8, 30, tzinfo=EASTERN))  # submitted, signal called once
    assert engine.called == 1
    r.run_iteration(datetime(2026, 5, 5, 8, 31, tzinfo=EASTERN))  # pending poll
    # Signal engine must not be called while pending.
    assert engine.called == 1
