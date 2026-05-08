import random
from datetime import time

import pytest

from src.slippage import (
    FillRequest,
    FillResult,
    SlippageModel,
    SlippageParams,
    effective_over_quoted,
)


def _base_request(**overrides) -> FillRequest:
    """Build a midday SPY entry request; override fields per-test."""
    defaults = dict(
        side="BUY",
        intent="entry",
        bid=1.00,
        ask=1.10,
        underlying_sigma_ann=0.18,
        delta=0.50,
        gamma=0.05,
        theta_per_day=-0.15,
        underlying_price=500.0,
        quote_age_ms=200,
        decision_to_submit_ms=200,
        submit_to_fill_ms=300,
        now_local_time=time(11, 0),
        symbol="SPY",
        qty=2,
        displayed_size=50,
        order_type="market",
        is_event_window=False,
    )
    defaults.update(overrides)
    return FillRequest(**defaults)


def _model(seed: int = 0) -> SlippageModel:
    return SlippageModel(rng=random.Random(seed))


def test_no_quote_returns_no_quote_status():
    model = _model()
    req = _base_request(bid=1.0, ask=1.0)
    result = model.estimate_fill(req)
    assert result.status == "no_quote"
    assert result.fill_price is None


def test_buy_fills_above_mid_for_market_order():
    model = _model()
    req = _base_request(side="BUY", order_type="market")
    result = model.estimate_fill(req)
    assert result.status == "filled"
    mid = 0.5 * (req.bid + req.ask)
    assert result.fill_price > mid


def test_sell_fills_below_mid_for_market_order():
    model = _model()
    req = _base_request(side="SELL", order_type="market")
    result = model.estimate_fill(req)
    assert result.status == "filled"
    mid = 0.5 * (req.bid + req.ask)
    assert result.fill_price < mid


def test_market_order_pays_more_than_marketable_limit_plus_tick():
    # Zero out random noise so we compare only the deterministic component;
    # the two order types consume the rng stream differently (the ML path
    # spends one extra draw on the fill-probability check), so a same-seed
    # comparison without this would be apples-to-oranges.
    params = SlippageParams(noise_sigma_frac_of_half_spread=0.0)
    market = SlippageModel(params=params, rng=random.Random(0)).estimate_fill(
        _base_request(order_type="market")
    )
    ml_tick = SlippageModel(params=params, rng=random.Random(0)).estimate_fill(
        _base_request(order_type="marketable_limit_at_mid_plus_tick")
    )
    assert market.status == "filled"
    assert ml_tick.status == "filled"
    assert market.fill_price > ml_tick.fill_price


def test_stop_intent_costs_more_than_entry_for_a_buy():
    entry = _model(seed=7).estimate_fill(_base_request(intent="entry", order_type="market"))
    stop = _model(seed=7).estimate_fill(_base_request(intent="stop", order_type="market"))
    assert entry.status == "filled" and stop.status == "filled"
    assert stop.fill_price > entry.fill_price


def test_event_window_costs_more_than_normal_window():
    base = _model(seed=11).estimate_fill(
        _base_request(order_type="market", is_event_window=False)
    )
    event = _model(seed=11).estimate_fill(
        _base_request(order_type="market", is_event_window=True)
    )
    assert event.fill_price > base.fill_price


def test_open_window_costs_more_than_midday():
    open_fill = _model(seed=3).estimate_fill(
        _base_request(order_type="market", now_local_time=time(9, 32))
    )
    mid_fill = _model(seed=3).estimate_fill(
        _base_request(order_type="market", now_local_time=time(11, 0))
    )
    assert open_fill.fill_price > mid_fill.fill_price


def test_last_five_minutes_costs_more_than_midday():
    last5 = _model(seed=5).estimate_fill(
        _base_request(order_type="market", now_local_time=time(15, 58))
    )
    mid = _model(seed=5).estimate_fill(
        _base_request(order_type="market", now_local_time=time(11, 0))
    )
    assert last5.fill_price > mid.fill_price


def test_per_symbol_kappa_widens_for_less_liquid_underlyings():
    spy = _model(seed=1).estimate_fill(_base_request(order_type="market", symbol="SPY"))
    gld = _model(seed=1).estimate_fill(_base_request(order_type="market", symbol="GLD"))
    assert gld.fill_price > spy.fill_price


def test_marketable_limit_at_mid_can_fail_to_fill():
    # Force the unfilled branch: zero fill probability.
    params = SlippageParams(ml_fill_prob_at_mid=0.0)
    model = SlippageModel(params=params, rng=random.Random(0))
    req = _base_request(order_type="marketable_limit_at_mid")
    result = model.estimate_fill(req)
    assert result.status == "unfilled_timeout"
    assert result.fill_price is None


def test_marketable_limit_at_mid_fills_when_probability_is_one():
    params = SlippageParams(ml_fill_prob_at_mid=1.0)
    model = SlippageModel(params=params, rng=random.Random(0))
    req = _base_request(order_type="marketable_limit_at_mid")
    result = model.estimate_fill(req)
    assert result.status == "filled"


def test_deterministic_with_seeded_rng():
    a = _model(seed=99).estimate_fill(_base_request(order_type="market"))
    b = _model(seed=99).estimate_fill(_base_request(order_type="market"))
    assert a.fill_price == pytest.approx(b.fill_price)


def test_quote_staleness_increases_latency_cost():
    fresh = _model(seed=4).estimate_fill(
        _base_request(order_type="market", quote_age_ms=100)
    )
    stale = _model(seed=4).estimate_fill(
        _base_request(order_type="market", quote_age_ms=10_000)
    )
    assert stale.fill_price > fresh.fill_price


def test_effective_over_quoted_zero_when_at_mid():
    assert effective_over_quoted(fill_price=1.05, mid=1.05, half_spread=0.05, side="BUY") == 0.0


def test_effective_over_quoted_one_when_buy_pays_full_ask():
    # Buying at the full ask = mid + half_spread → EFQ = 1.0.
    assert effective_over_quoted(fill_price=1.10, mid=1.05, half_spread=0.05, side="BUY") == pytest.approx(1.0)


def test_effective_over_quoted_one_when_sell_takes_full_bid():
    # Selling at the full bid = mid - half_spread → signed EFQ = +1.0.
    assert effective_over_quoted(fill_price=1.00, mid=1.05, half_spread=0.05, side="SELL") == pytest.approx(1.0)


def test_clamp_keeps_fill_within_one_tick_of_quotes():
    # An extreme stop intent in event window expands the clamp; in normal
    # conditions the fill price should sit between bid - tick and ask + tick.
    model = _model(seed=21)
    req = _base_request(order_type="market", is_event_window=False)
    result = model.estimate_fill(req)
    assert result.fill_price >= req.bid - model.params.tick_size
    assert result.fill_price <= req.ask + model.params.tick_size
