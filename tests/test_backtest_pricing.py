from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.backtest.pricing import (
    EASTERN,
    next_session_date,
    price_atm_option,
    price_existing_option,
    round_to_strike,
    time_to_next_close_years,
    time_to_target_close_years,
)


def test_round_to_strike_default_dollar_increment():
    assert round_to_strike(500.4) == 500.0
    assert round_to_strike(500.6) == 501.0


def test_round_to_strike_custom_increment():
    assert round_to_strike(500.7, increment=5.0) == 500.0
    assert round_to_strike(503.0, increment=5.0) == 505.0


def test_round_to_strike_rejects_zero_increment():
    with pytest.raises(ValueError):
        round_to_strike(500.0, increment=0.0)


def test_time_to_next_close_at_market_open():
    # 09:30 ET on a Mon → next-day 16:00 ET = 30.5 hours later
    t = datetime(2026, 5, 4, 9, 30, tzinfo=EASTERN)
    years = time_to_next_close_years(t)
    expected = (30.5 * 3600.0) / (365.0 * 24 * 3600.0)
    assert years == pytest.approx(expected, rel=1e-6)


def test_time_to_next_close_at_market_close():
    # 16:00 ET → tomorrow 16:00 ET = 24 hours
    t = datetime(2026, 5, 4, 16, 0, tzinfo=EASTERN)
    years = time_to_next_close_years(t)
    expected = (24.0 * 3600.0) / (365.0 * 24 * 3600.0)
    assert years == pytest.approx(expected, rel=1e-6)


def test_time_to_next_close_handles_naive_datetime():
    naive = datetime(2026, 5, 4, 12, 0)
    years = time_to_next_close_years(naive)
    assert years > 0


def test_time_to_target_close_same_day():
    t = datetime(2026, 5, 4, 9, 30, tzinfo=EASTERN)
    years = time_to_target_close_years(t, "2026-05-04")
    expected = (6.5 * 3600.0) / (365.0 * 24 * 3600.0)
    assert years == pytest.approx(expected, rel=1e-6)


def test_time_to_target_close_zero_when_past_expiration():
    t = datetime(2026, 5, 5, 9, 30, tzinfo=EASTERN)
    assert time_to_target_close_years(t, "2026-05-04") == 0.0


def test_next_session_date_advances_one_day():
    t = datetime(2026, 5, 4, 12, 0, tzinfo=EASTERN)
    assert next_session_date(t) == "2026-05-05"


def test_price_atm_option_call_sane_for_1dte_spy_like():
    underlying = 500.0
    T = 30.5 * 3600.0 / (365.0 * 24 * 3600.0)  # ~30.5h
    strike, greeks = price_atm_option(
        underlying=underlying, direction="CALL",
        time_to_expiry_years=T, iv=0.18,
    )
    assert strike == pytest.approx(500.0)
    assert 0.50 < greeks.price < 5.00
    assert 0.0 < greeks.delta < 1.0
    assert greeks.gamma > 0.0
    assert greeks.theta < 0.0


def test_price_atm_option_put_sane_for_1dte():
    strike, greeks = price_atm_option(
        underlying=500.0, direction="PUT",
        time_to_expiry_years=0.00348, iv=0.18,
    )
    assert strike == 500.0
    assert greeks.price > 0.0
    assert -1.0 < greeks.delta < 0.0


def test_price_atm_option_uses_strike_increment():
    strike, _ = price_atm_option(
        underlying=502.7, direction="CALL",
        time_to_expiry_years=0.00348, iv=0.18,
        strike_increment=5.0,
    )
    assert strike == 505.0


def test_price_existing_option_decays_as_T_shrinks():
    args = dict(underlying=500.0, strike=500.0, direction="CALL", iv=0.18)
    early = price_existing_option(time_to_expiry_years=0.00348, **args)
    late = price_existing_option(time_to_expiry_years=0.00100, **args)
    assert early.price > late.price


def test_price_existing_option_responds_to_underlying_move():
    args = dict(strike=500.0, direction="CALL", time_to_expiry_years=0.00348, iv=0.18)
    flat = price_existing_option(underlying=500.0, **args)
    up = price_existing_option(underlying=502.0, **args)
    down = price_existing_option(underlying=498.0, **args)
    assert up.price > flat.price > down.price
