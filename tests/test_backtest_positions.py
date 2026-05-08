from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.backtest.positions import (
    ClosedTrade,
    OpenPosition,
    close_position,
    evaluate_exit,
    realized_pnl,
)


EASTERN = ZoneInfo("America/New_York")


def _make_open_position(**overrides) -> OpenPosition:
    defaults = dict(
        symbol="SPY",
        direction="CALL",
        strike=500.0,
        expiration_date="2026-05-05",
        contracts=2,
        entry_time_et=datetime(2026, 5, 4, 10, 0, tzinfo=EASTERN),
        entry_price=1.50,
        entry_underlying=500.0,
        entry_iv=0.18,
        entry_delta=0.50,
        entry_gamma=0.05,
        entry_theta_per_day=-0.15,
        take_profit_pct=0.30,
        stop_loss_pct=0.25,
        max_hold_minutes=120,
    )
    defaults.update(overrides)
    return OpenPosition(**defaults)


def test_realized_pnl_positive_for_winning_trade():
    assert realized_pnl(entry_price=1.0, exit_price=1.50, contracts=2) == pytest.approx(100.0)


def test_realized_pnl_negative_for_losing_trade():
    assert realized_pnl(entry_price=1.0, exit_price=0.75, contracts=3) == pytest.approx(-75.0)


def test_realized_pnl_respects_contract_multiplier():
    assert realized_pnl(entry_price=1.0, exit_price=2.0, contracts=1, contract_multiplier=50) == pytest.approx(50.0)


def test_close_position_yields_correct_holding_time_and_pnl():
    position = _make_open_position()
    exit_time = datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN)
    closed = close_position(
        position, exit_time_et=exit_time, exit_price=2.0, exit_reason="tp"
    )
    assert isinstance(closed, ClosedTrade)
    assert closed.holding_minutes == pytest.approx(30.0)
    # 0.50 * 2 * 100 = 100
    assert closed.realized_pnl == pytest.approx(100.0)
    assert closed.exit_reason == "tp"


def test_evaluate_exit_take_profit_takes_precedence():
    position = _make_open_position()
    reason = evaluate_exit(
        position,
        current_option_price=2.0,        # +33%
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=200.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"


def test_evaluate_exit_stop_loss_triggers():
    position = _make_open_position()
    reason = evaluate_exit(
        position,
        current_option_price=1.0,        # -33%
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=200.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "stop"


def test_evaluate_exit_session_close_buffer():
    position = _make_open_position()
    reason = evaluate_exit(
        position,
        current_option_price=1.55,       # neither TP nor SL
        current_time_et=datetime(2026, 5, 4, 15, 56, tzinfo=EASTERN),
        minutes_to_session_close=4.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "session_close"


def test_evaluate_exit_time_stop_after_max_hold():
    position = _make_open_position(max_hold_minutes=30)
    reason = evaluate_exit(
        position,
        current_option_price=1.55,
        current_time_et=datetime(2026, 5, 4, 11, 0, tzinfo=EASTERN),  # 60min after entry
        minutes_to_session_close=200.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "time_stop"


def test_evaluate_exit_returns_none_when_no_trigger():
    position = _make_open_position()
    reason = evaluate_exit(
        position,
        current_option_price=1.55,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=200.0,
        exit_before_close_minutes=5.0,
    )
    assert reason is None


def test_evaluate_exit_tp_outranks_stop_when_both_borderline():
    # Construct contrived case where price somehow satisfies both — TP precedence wins.
    position = _make_open_position(take_profit_pct=0.30, stop_loss_pct=0.05)
    reason = evaluate_exit(
        position,
        current_option_price=2.0,        # +33% triggers TP
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=200.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"


def test_open_position_is_frozen():
    pos = _make_open_position()
    with pytest.raises(Exception):
        pos.entry_price = 99.0  # type: ignore[misc]
