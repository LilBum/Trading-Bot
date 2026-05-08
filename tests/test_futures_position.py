from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.futures_position import (
    FuturesClosedTrade,
    FuturesOpenPosition,
    close_position,
    evaluate_exit,
    evaluate_exit_intrabar,
    realized_pnl_dollars,
    realized_pnl_points,
)


EASTERN = ZoneInfo("America/New_York")


def _make_open_position(**overrides) -> FuturesOpenPosition:
    defaults = dict(
        symbol="ES",
        side="BUY",
        contracts=1,
        entry_time_et=datetime(2026, 5, 4, 10, 0, tzinfo=EASTERN),
        entry_price=4500.0,
        point_value=50.0,
        tick_size=0.25,
        take_profit_points=8.0,   # +8 ES points = $400 on 1 contract
        stop_loss_points=4.0,     # -4 ES points = -$200 on 1 contract
        max_hold_minutes=120,
    )
    defaults.update(overrides)
    return FuturesOpenPosition(**defaults)


# ----- realized_pnl helpers ---------------------------------------------


def test_long_pnl_points_positive_when_exit_above_entry():
    assert realized_pnl_points("BUY", 4500.0, 4510.0, 1) == pytest.approx(10.0)


def test_long_pnl_points_negative_when_exit_below_entry():
    assert realized_pnl_points("BUY", 4500.0, 4490.0, 1) == pytest.approx(-10.0)


def test_short_pnl_points_positive_when_exit_below_entry():
    assert realized_pnl_points("SELL", 4500.0, 4490.0, 1) == pytest.approx(10.0)


def test_short_pnl_points_negative_when_exit_above_entry():
    assert realized_pnl_points("SELL", 4500.0, 4510.0, 1) == pytest.approx(-10.0)


def test_pnl_dollars_for_es():
    assert realized_pnl_dollars("BUY", 4500.0, 4510.0, 1, 50.0) == pytest.approx(500.0)


def test_pnl_dollars_for_nq():
    assert realized_pnl_dollars("BUY", 15000.0, 15010.0, 1, 20.0) == pytest.approx(200.0)


def test_pnl_dollars_scales_with_contracts():
    assert realized_pnl_dollars("BUY", 4500.0, 4510.0, 3, 50.0) == pytest.approx(1500.0)


# ----- close_position ---------------------------------------------------


def test_close_position_long_winning_trade():
    pos = _make_open_position()
    exit_time = datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN)
    closed = close_position(pos, exit_time_et=exit_time, exit_price=4508.0, exit_reason="tp")
    assert isinstance(closed, FuturesClosedTrade)
    assert closed.holding_minutes == pytest.approx(30.0)
    assert closed.realized_points == pytest.approx(8.0)
    assert closed.realized_pnl == pytest.approx(400.0)  # 8 * $50
    assert closed.exit_reason == "tp"


def test_close_position_short_winning_trade():
    pos = _make_open_position(side="SELL", entry_price=15000.0, point_value=20.0)
    closed = close_position(
        pos,
        exit_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        exit_price=14990.0,
        exit_reason="tp",
    )
    assert closed.realized_points == pytest.approx(10.0)
    assert closed.realized_pnl == pytest.approx(200.0)


def test_close_position_long_losing_trade():
    pos = _make_open_position()
    closed = close_position(
        pos,
        exit_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        exit_price=4496.0,
        exit_reason="stop",
    )
    assert closed.realized_points == pytest.approx(-4.0)
    assert closed.realized_pnl == pytest.approx(-200.0)


# ----- evaluate_exit ----------------------------------------------------


def test_long_take_profit_triggers():
    pos = _make_open_position()
    reason = evaluate_exit(
        pos,
        current_price=4508.0,        # +8 points = TP threshold
        current_time_et=datetime(2026, 5, 4, 10, 5, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"


def test_long_stop_loss_triggers():
    pos = _make_open_position()
    reason = evaluate_exit(
        pos,
        current_price=4496.0,        # -4 points = stop threshold
        current_time_et=datetime(2026, 5, 4, 10, 5, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "stop"


def test_short_take_profit_triggers_when_price_drops():
    pos = _make_open_position(side="SELL")
    reason = evaluate_exit(
        pos,
        current_price=4492.0,        # -8 points (favorable for short)
        current_time_et=datetime(2026, 5, 4, 10, 5, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"


def test_short_stop_loss_triggers_when_price_rises():
    pos = _make_open_position(side="SELL")
    reason = evaluate_exit(
        pos,
        current_price=4504.0,        # +4 points (adverse for short)
        current_time_et=datetime(2026, 5, 4, 10, 5, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "stop"


def test_session_close_buffer_triggers_before_close():
    pos = _make_open_position()
    reason = evaluate_exit(
        pos,
        current_price=4502.0,        # neither TP nor stop
        current_time_et=datetime(2026, 5, 4, 15, 56, tzinfo=EASTERN),
        minutes_to_session_close=4.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "session_close"


def test_time_stop_triggers_after_max_hold():
    pos = _make_open_position(max_hold_minutes=30)
    reason = evaluate_exit(
        pos,
        current_price=4502.0,
        current_time_et=datetime(2026, 5, 4, 11, 0, tzinfo=EASTERN),  # 60 min after entry
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "time_stop"


def test_evaluate_exit_returns_none_when_no_trigger():
    pos = _make_open_position()
    reason = evaluate_exit(
        pos,
        current_price=4502.0,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason is None


def test_tp_outranks_stop_when_both_borderline():
    # Contrived: tp_points small, sl_points small; price hits both nominally.
    pos = _make_open_position(take_profit_points=1.0, stop_loss_points=0.5)
    reason = evaluate_exit(
        pos,
        current_price=4502.0,        # +2 points triggers TP precedence
        current_time_et=datetime(2026, 5, 4, 10, 5, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"


# ----- intrabar exit evaluation ------------------------------------------


def test_intrabar_long_tp_fires_when_high_crosses_threshold():
    """Bar high reaches TP even though close pulled back."""
    pos = _make_open_position(side="BUY", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4509.0,   # crosses TP at 4508
        bar_low=4499.0,    # didn't cross SL at 4496
        bar_close=4503.0,  # close-only logic would miss TP
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"
    assert price == pytest.approx(4508.0)


def test_intrabar_long_stop_fires_when_low_crosses_threshold():
    pos = _make_open_position(side="BUY", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4502.0,
        bar_low=4495.0,    # crosses SL at 4496
        bar_close=4500.5,  # close-only would miss
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "stop"
    assert price == pytest.approx(4496.0)


def test_intrabar_long_both_hit_prefers_stop_by_default():
    """If both TP and SL crossed in same bar, conservative call: stop fired first."""
    pos = _make_open_position(side="BUY", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4510.0,   # crosses TP
        bar_low=4495.0,    # also crosses SL
        bar_close=4502.0,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "stop"
    assert price == pytest.approx(4496.0)


def test_intrabar_long_both_hit_can_prefer_tp_when_optimistic():
    pos = _make_open_position(side="BUY", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4510.0,
        bar_low=4495.0,
        bar_close=4502.0,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
        prefer_stop_when_both_hit=False,
    )
    assert reason == "tp"
    assert price == pytest.approx(4508.0)


def test_intrabar_short_tp_fires_when_low_crosses_threshold():
    """Short TP is below entry — bar low must reach it."""
    pos = _make_open_position(side="SELL", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4502.0,
        bar_low=4491.0,    # crosses TP at 4492
        bar_close=4500.5,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "tp"
    assert price == pytest.approx(4492.0)


def test_intrabar_short_stop_fires_when_high_crosses_threshold():
    pos = _make_open_position(side="SELL", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4505.0,   # crosses SL at 4504
        bar_low=4499.0,
        bar_close=4501.0,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "stop"
    assert price == pytest.approx(4504.0)


def test_intrabar_returns_session_close_with_bar_close_price():
    pos = _make_open_position(side="BUY", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4502.0, bar_low=4499.0, bar_close=4501.5,
        current_time_et=datetime(2026, 5, 4, 15, 58, tzinfo=EASTERN),
        minutes_to_session_close=2.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "session_close"
    assert price == pytest.approx(4501.5)


def test_intrabar_returns_time_stop_when_max_hold_exceeded():
    pos = _make_open_position(
        side="BUY", entry_price=4500.0,
        take_profit_points=8.0, stop_loss_points=4.0,
        max_hold_minutes=30,
        entry_time_et=datetime(2026, 5, 4, 10, 0, tzinfo=EASTERN),
    )
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4502.0, bar_low=4499.0, bar_close=4501.0,
        current_time_et=datetime(2026, 5, 4, 11, 0, tzinfo=EASTERN),  # 60 min held
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason == "time_stop"
    assert price == pytest.approx(4501.0)


def test_intrabar_returns_no_exit_when_within_range_and_under_time_limit():
    pos = _make_open_position(side="BUY", entry_price=4500.0,
                              take_profit_points=8.0, stop_loss_points=4.0)
    reason, price = evaluate_exit_intrabar(
        pos,
        bar_high=4505.0, bar_low=4498.0, bar_close=4502.0,
        current_time_et=datetime(2026, 5, 4, 10, 30, tzinfo=EASTERN),
        minutes_to_session_close=300.0,
        exit_before_close_minutes=5.0,
    )
    assert reason is None
    assert price is None


# ----- frozenness --------------------------------------------------------


def test_open_position_is_frozen():
    pos = _make_open_position()
    with pytest.raises(Exception):
        pos.entry_price = 9999.0  # type: ignore[misc]
