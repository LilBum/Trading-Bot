import math

import pytest

from src.synthetic_options import (
    IVProxyParams,
    OptionGreeks,
    SpreadParams,
    black_scholes,
    estimate_spread_pct,
    realized_vol_iv_proxy,
    synthetic_bid_ask,
)


# ----- Black-Scholes core -----------------------------------------------


def test_bs_call_atm_matches_known_reference_value():
    # Standard textbook example: S=K=100, T=0.25y, σ=20%, r=5% → C ≈ 4.6150.
    g = black_scholes(
        underlying=100.0,
        strike=100.0,
        time_to_expiry_years=0.25,
        iv=0.20,
        risk_free_rate=0.05,
        option_type="CALL",
    )
    assert g.price == pytest.approx(4.6150, abs=0.01)


def test_bs_put_call_parity():
    # C - P = S - K * exp(-r*T)
    args = dict(
        underlying=105.0,
        strike=100.0,
        time_to_expiry_years=0.10,
        iv=0.25,
        risk_free_rate=0.04,
    )
    call = black_scholes(option_type="CALL", **args)
    put = black_scholes(option_type="PUT", **args)
    parity = args["underlying"] - args["strike"] * math.exp(
        -args["risk_free_rate"] * args["time_to_expiry_years"]
    )
    assert (call.price - put.price) == pytest.approx(parity, abs=1e-6)


def test_bs_call_delta_in_unit_interval():
    g = black_scholes(
        underlying=100.0, strike=100.0, time_to_expiry_years=0.05, iv=0.30, option_type="CALL"
    )
    assert 0.0 < g.delta < 1.0


def test_bs_put_delta_in_negative_unit_interval():
    g = black_scholes(
        underlying=100.0, strike=100.0, time_to_expiry_years=0.05, iv=0.30, option_type="PUT"
    )
    assert -1.0 < g.delta < 0.0


def test_bs_deep_itm_call_delta_approaches_one():
    g = black_scholes(
        underlying=200.0, strike=100.0, time_to_expiry_years=0.10, iv=0.20, option_type="CALL"
    )
    assert g.delta > 0.99


def test_bs_deep_otm_call_delta_approaches_zero():
    g = black_scholes(
        underlying=50.0, strike=100.0, time_to_expiry_years=0.10, iv=0.20, option_type="CALL"
    )
    assert g.delta < 0.01


def test_bs_gamma_is_positive_at_atm():
    g = black_scholes(
        underlying=100.0, strike=100.0, time_to_expiry_years=0.10, iv=0.20, option_type="CALL"
    )
    assert g.gamma > 0.0


def test_bs_vega_is_positive():
    g = black_scholes(
        underlying=100.0, strike=100.0, time_to_expiry_years=0.10, iv=0.20, option_type="CALL"
    )
    assert g.vega > 0.0


def test_bs_theta_is_negative_for_long_options():
    g = black_scholes(
        underlying=100.0, strike=100.0, time_to_expiry_years=0.10, iv=0.20, option_type="CALL"
    )
    assert g.theta < 0.0


def test_bs_zero_T_returns_intrinsic():
    g = black_scholes(
        underlying=110.0, strike=100.0, time_to_expiry_years=0.0, iv=0.30, option_type="CALL"
    )
    assert g.price == pytest.approx(10.0, abs=1e-9)
    assert g.delta == 1.0
    assert g.gamma == 0.0
    assert g.theta == 0.0


def test_bs_zero_iv_returns_intrinsic_for_otm_put():
    g = black_scholes(
        underlying=110.0, strike=100.0, time_to_expiry_years=0.10, iv=0.0, option_type="PUT"
    )
    assert g.price == 0.0
    assert g.delta == 0.0


def test_bs_1dte_atm_price_in_sane_range():
    # SPY-like: $500 underlying, 18% IV, 1 calendar day → straddle leg
    # in roughly $1-3 territory. Sanity bound.
    g = black_scholes(
        underlying=500.0,
        strike=500.0,
        time_to_expiry_years=1.0 / 365.0,
        iv=0.18,
        risk_free_rate=0.04,
        option_type="CALL",
    )
    assert 0.50 < g.price < 5.00


# ----- Spread proxy -----------------------------------------------------


def test_estimate_spread_pct_low_premium_tier():
    assert estimate_spread_pct(0.30) == pytest.approx(0.20)


def test_estimate_spread_pct_mid_premium_tier():
    assert estimate_spread_pct(1.50) == pytest.approx(0.12)


def test_estimate_spread_pct_high_premium_tier():
    assert estimate_spread_pct(10.00) == pytest.approx(0.05)


def test_estimate_spread_pct_above_top_tier():
    assert estimate_spread_pct(50.00) == pytest.approx(0.04)


def test_estimate_spread_pct_decreases_monotonically():
    samples = [estimate_spread_pct(p) for p in (0.30, 0.75, 1.50, 3.00, 10.00, 50.00)]
    assert samples == sorted(samples, reverse=True)


def test_synthetic_bid_ask_brackets_mid():
    bid, ask = synthetic_bid_ask(2.00)
    assert bid < 2.00 < ask
    assert ask - bid == pytest.approx(2.00 * 0.12, abs=1e-9)


def test_synthetic_bid_ask_floors_bid_at_one_cent():
    # A $0.05 mid with 20% spread would push bid below zero — floor at 0.01.
    bid, _ = synthetic_bid_ask(0.05)
    assert bid >= 0.01


def test_synthetic_bid_ask_uses_custom_params():
    custom = SpreadParams(tiers=((float("inf"), 0.50),))
    bid, ask = synthetic_bid_ask(2.00, params=custom)
    assert ask - bid == pytest.approx(1.00, abs=1e-9)


# ----- IV proxy ---------------------------------------------------------


def test_realized_vol_proxy_floor_for_too_few_returns():
    p = IVProxyParams(floor=0.05)
    assert realized_vol_iv_proxy([], params=p) == 0.05
    assert realized_vol_iv_proxy([0.01], params=p) == 0.05


def test_realized_vol_proxy_zero_returns_floors_to_minimum():
    p = IVProxyParams(floor=0.10)
    assert realized_vol_iv_proxy([0.0] * 30, params=p) == 0.10


def test_realized_vol_proxy_recovers_known_daily_volatility():
    # 1% daily vol → annualized = 0.01 * sqrt(252) ≈ 0.1587. With VRP=1.0
    # the proxy should land near that. Use an alternating series to set the
    # exact stdev: returns of +1% and -1% have stdev = 0.01.
    returns = [0.01, -0.01] * 50
    p = IVProxyParams(bars_per_year=252.0, vrp_multiplier=1.0, floor=0.0, ceiling=10.0)
    iv = realized_vol_iv_proxy(returns, params=p)
    assert iv == pytest.approx(0.01 * math.sqrt(252.0), rel=0.05)


def test_realized_vol_proxy_applies_vrp_multiplier():
    returns = [0.01, -0.01] * 50
    base_params = IVProxyParams(bars_per_year=252.0, vrp_multiplier=1.0, floor=0.0, ceiling=10.0)
    bumped_params = IVProxyParams(bars_per_year=252.0, vrp_multiplier=1.5, floor=0.0, ceiling=10.0)
    base = realized_vol_iv_proxy(returns, params=base_params)
    bumped = realized_vol_iv_proxy(returns, params=bumped_params)
    assert bumped == pytest.approx(base * 1.5, rel=1e-6)


def test_realized_vol_proxy_clamps_to_ceiling():
    # Wild returns to push estimate above the ceiling.
    returns = [0.5, -0.5] * 50
    p = IVProxyParams(bars_per_year=252.0, vrp_multiplier=1.0, floor=0.0, ceiling=2.0)
    assert realized_vol_iv_proxy(returns, params=p) == 2.0


# ----- OptionGreeks dataclass -------------------------------------------


def test_option_greeks_is_immutable():
    g = OptionGreeks(price=1.0, delta=0.5, gamma=0.1, theta=-0.05, vega=0.2)
    with pytest.raises(Exception):
        g.price = 2.0  # type: ignore[misc]
