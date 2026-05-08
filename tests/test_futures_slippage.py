import random
from datetime import time

import pytest

from src.futures_slippage import (
    CONTRACTS,
    FuturesContract,
    FuturesFillRequest,
    FuturesFillResult,
    FuturesSlippageModel,
    FuturesSlippageParams,
    realized_pnl_dollars,
    realized_pnl_points,
)


def _base_request(**overrides) -> FuturesFillRequest:
    """Liquid midday ES market-order entry; tests override per case."""
    defaults = dict(
        side="BUY",
        intent="entry",
        bid=4500.00,
        ask=4500.25,           # 1 tick spread (ES standard)
        underlying_sigma_ann=0.18,
        quote_age_ms=100,
        decision_to_submit_ms=200,
        submit_to_fill_ms=300,
        now_local_time=time(11, 0),  # midday calm
        symbol="ES",
        qty=1,
        order_type="market",
        is_economic_release=False,
    )
    defaults.update(overrides)
    return FuturesFillRequest(**defaults)


def _model(seed: int = 0) -> FuturesSlippageModel:
    return FuturesSlippageModel(rng=random.Random(seed))


# ----- contract specs ---------------------------------------------------


def test_es_contract_spec_has_correct_point_value():
    es = CONTRACTS["ES"]
    assert es.tick_size == pytest.approx(0.25)
    assert es.point_value == pytest.approx(50.0)
    assert es.tick_value == pytest.approx(12.50)


def test_nq_contract_spec_has_correct_point_value():
    nq = CONTRACTS["NQ"]
    assert nq.tick_size == pytest.approx(0.25)
    assert nq.point_value == pytest.approx(20.0)
    assert nq.tick_value == pytest.approx(5.00)


def test_micro_contracts_have_one_tenth_point_value():
    assert CONTRACTS["MES"].point_value == pytest.approx(5.0)
    assert CONTRACTS["MNQ"].point_value == pytest.approx(2.0)


# ----- estimate_fill: core behavior -------------------------------------


def test_no_quote_returns_no_quote_status():
    model = _model()
    req = _base_request(bid=4500.0, ask=4500.0)  # zero-width
    result = model.estimate_fill(req)
    assert result.status == "no_quote"
    assert result.fill_price is None


def test_buy_market_order_fills_at_or_above_mid():
    model = _model()
    req = _base_request(side="BUY", order_type="market")
    result = model.estimate_fill(req)
    assert result.status == "filled"
    mid = 0.5 * (req.bid + req.ask)
    assert result.fill_price >= mid - 0.001  # within rounding


def test_sell_market_order_fills_at_or_below_mid():
    model = _model()
    req = _base_request(side="SELL", order_type="market")
    result = model.estimate_fill(req)
    assert result.status == "filled"
    mid = 0.5 * (req.bid + req.ask)
    assert result.fill_price <= mid + 0.001


def test_unknown_symbol_falls_back_to_minimum_spec():
    """Unknown symbol shouldn't crash; uses a fallback spec with tiny ticks."""
    model = _model()
    req = _base_request(symbol="ZZZ")
    result = model.estimate_fill(req)
    assert result.status == "filled"
    assert result.fill_price is not None


# ----- intent and timing kickers ----------------------------------------


def test_stop_intent_costs_more_than_entry_for_a_buy():
    entry = _model(seed=7).estimate_fill(_base_request(intent="entry", order_type="market"))
    stop = _model(seed=7).estimate_fill(_base_request(intent="stop", order_type="market"))
    assert entry.fill_price is not None and stop.fill_price is not None
    assert stop.fill_price > entry.fill_price


def test_economic_release_widens_slippage():
    base = _model(seed=11).estimate_fill(
        _base_request(order_type="market", is_economic_release=False)
    )
    release = _model(seed=11).estimate_fill(
        _base_request(order_type="market", is_economic_release=True)
    )
    assert release.fill_price > base.fill_price


def test_overnight_costs_more_than_midday():
    overnight = _model(seed=3).estimate_fill(
        _base_request(order_type="market", now_local_time=time(3, 0))
    )
    midday = _model(seed=3).estimate_fill(
        _base_request(order_type="market", now_local_time=time(11, 0))
    )
    assert overnight.fill_price > midday.fill_price


def test_open_spike_costs_more_than_midday():
    open_fill = _model(seed=5).estimate_fill(
        _base_request(order_type="market", now_local_time=time(9, 32))
    )
    midday = _model(seed=5).estimate_fill(
        _base_request(order_type="market", now_local_time=time(11, 0))
    )
    assert open_fill.fill_price > midday.fill_price


# ----- order-type sensitivity -------------------------------------------


def test_market_pays_more_than_marketable_limit_at_one_tick_through():
    params = FuturesSlippageParams(noise_sigma_frac_of_tick=0.0)
    market = FuturesSlippageModel(params=params, rng=random.Random(0)).estimate_fill(
        _base_request(order_type="market")
    )
    ml = FuturesSlippageModel(params=params, rng=random.Random(0)).estimate_fill(
        _base_request(order_type="marketable_limit_at_one_tick_through")
    )
    assert market.fill_price > ml.fill_price


def test_marketable_limit_at_mid_can_fail_to_fill():
    params = FuturesSlippageParams(ml_fill_prob_at_mid=0.0)
    model = FuturesSlippageModel(params=params, rng=random.Random(0))
    result = model.estimate_fill(_base_request(order_type="marketable_limit_at_mid"))
    assert result.status == "unfilled_timeout"
    assert result.fill_price is None


def test_marketable_limit_at_mid_fills_when_probability_is_one():
    params = FuturesSlippageParams(ml_fill_prob_at_mid=1.0)
    model = FuturesSlippageModel(params=params, rng=random.Random(0))
    result = model.estimate_fill(_base_request(order_type="marketable_limit_at_mid"))
    assert result.status == "filled"


# ----- determinism with seeded rng --------------------------------------


def test_deterministic_with_seeded_rng():
    a = _model(seed=99).estimate_fill(_base_request())
    b = _model(seed=99).estimate_fill(_base_request())
    assert a.fill_price == pytest.approx(b.fill_price)


def test_quote_staleness_increases_drift_cost():
    fresh = _model(seed=4).estimate_fill(
        _base_request(order_type="market", quote_age_ms=100)
    )
    stale = _model(seed=4).estimate_fill(
        _base_request(order_type="market", quote_age_ms=10_000)
    )
    assert stale.fill_price > fresh.fill_price


# ----- realized PnL helpers ---------------------------------------------


def test_long_pnl_points_positive_when_exit_above_entry():
    assert realized_pnl_points(4500.0, 4510.0, "BUY", 1) == pytest.approx(10.0)


def test_long_pnl_points_negative_when_exit_below_entry():
    assert realized_pnl_points(4500.0, 4490.0, "BUY", 1) == pytest.approx(-10.0)


def test_short_pnl_points_positive_when_exit_below_entry():
    assert realized_pnl_points(4500.0, 4490.0, "SELL", 1) == pytest.approx(10.0)


def test_short_pnl_points_negative_when_exit_above_entry():
    assert realized_pnl_points(4500.0, 4510.0, "SELL", 1) == pytest.approx(-10.0)


def test_pnl_dollars_for_es_uses_50_dollar_point_value():
    es_value = 50.0
    # +10 points on 1 ES contract = $500.
    assert realized_pnl_dollars(4500.0, 4510.0, "BUY", 1, es_value) == pytest.approx(500.0)


def test_pnl_dollars_scales_with_contracts():
    es_value = 50.0
    assert realized_pnl_dollars(4500.0, 4510.0, "BUY", 3, es_value) == pytest.approx(1500.0)


def test_pnl_dollars_for_nq():
    nq_value = 20.0
    # +10 points on 1 NQ contract = $200.
    assert realized_pnl_dollars(15000.0, 15010.0, "BUY", 1, nq_value) == pytest.approx(200.0)
