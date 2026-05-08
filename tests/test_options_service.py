import pandas as pd

from src.services.options_service import OptionInstrumentService


def test_option_scoring_selects_best_candidate():
    options_cfg = {
        "moneyness_preference": "ATM_OR_1ITM",
        "min_open_interest": 100,
        "min_volume": 50,
        "max_spread_pct": 0.2,
        "min_option_price": 0.2,
        "max_option_price": 15.0,
        "delta_target": 0.5,
        "delta_tolerance": 0.3,
        "oi_score_scale": 3.0,
        "volume_score_scale": 3.0,
        "scoring": {
            "weight_spread": 0.4,
            "weight_oi": 0.2,
            "weight_volume": 0.2,
            "weight_delta": 0.15,
            "weight_price": 0.05,
        },
    }
    service = OptionInstrumentService(options_cfg)
    chain = pd.DataFrame(
        [
            {
                "strike": 100.0,
                "bid": 1.0,
                "ask": 1.2,
                "open_interest": 500,
                "volume": 200,
                "last_price": 1.1,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.25,
            },
            {
                "strike": 101.0,
                "bid": 0.9,
                "ask": 1.5,
                "open_interest": 150,
                "volume": 60,
                "last_price": 1.2,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.25,
            },
        ]
    )

    selection = service.select_contract(
        "GLD",
        ("2024-01-03", chain),
        "CALL",
        100.5,
        "2024-01-02T15:00:00+00:00",
    )

    assert selection.option_contract is not None
    assert selection.option_contract.strike == 100.0
    assert len(selection.top_candidates) >= 1


def test_option_rejects_stale_quotes():
    options_cfg = {
        "moneyness_preference": "ATM_OR_1ITM",
        "min_open_interest": 100,
        "min_volume": 50,
        "max_spread_pct": 0.2,
        "min_option_price": 0.2,
        "max_option_price": 15.0,
        "max_quote_age_minutes": 1,
        "delta_target": 0.5,
        "delta_tolerance": 0.3,
    }
    service = OptionInstrumentService(options_cfg)
    chain = pd.DataFrame(
        [
            {
                "strike": 100.0,
                "bid": 1.0,
                "ask": 1.2,
                "open_interest": 500,
                "volume": 200,
                "last_price": 1.1,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.25,
                "lastTradeDate": "2024-01-01T12:00:00Z",
            }
        ]
    )

    selection = service.select_contract(
        "GLD",
        ("2024-01-03", chain),
        "CALL",
        100.5,
        "2024-01-02T15:00:00+00:00",
    )

    assert selection.option_contract is None
    assert "No contracts passed liquidity filters" in selection.reject_reasons


def test_missing_iv_is_hard_reject_for_short_dte():
    options_cfg = {
        "moneyness_preference": "ATM_OR_1ITM",
        "min_open_interest": 100,
        "min_volume": 50,
        "max_spread_pct": 0.2,
        "min_option_price": 0.2,
        "max_option_price": 15.0,
        "require_iv_for_short_dte": True,
        "short_dte_threshold_days": 2,
    }
    service = OptionInstrumentService(options_cfg)
    chain = pd.DataFrame(
        [
            {
                "strike": 100.0,
                "bid": 1.0,
                "ask": 1.2,
                "open_interest": 500,
                "volume": 200,
                "last_price": 1.1,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.0,
            }
        ]
    )

    selection = service.select_contract(
        "GLD",
        ("2024-01-03", chain),
        "CALL",
        100.5,
        "2024-01-02T15:00:00+00:00",
    )

    assert selection.option_contract is None
    assert "No contracts passed liquidity filters" in selection.reject_reasons


def test_iv_deviation_hard_reject():
    options_cfg = {
        "moneyness_preference": "ATM_OR_1ITM",
        "min_open_interest": 100,
        "min_volume": 50,
        "max_spread_pct": 0.2,
        "min_option_price": 0.2,
        "max_option_price": 15.0,
        "iv_deviation_pct_max": 10,
        "iv_deviation_hard_reject": True,
    }
    service = OptionInstrumentService(options_cfg)
    chain = pd.DataFrame(
        [
            {
                "strike": 100.0,
                "bid": 1.0,
                "ask": 1.2,
                "open_interest": 500,
                "volume": 200,
                "last_price": 1.1,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.2,
            },
            {
                "strike": 101.0,
                "bid": 1.0,
                "ask": 1.2,
                "open_interest": 500,
                "volume": 200,
                "last_price": 1.1,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.5,
            },
        ]
    )

    selection = service.select_contract(
        "GLD",
        ("2024-01-03", chain),
        "CALL",
        100.5,
        "2024-01-02T15:00:00+00:00",
    )

    assert selection.option_contract is None
    assert "No contracts passed liquidity filters" in selection.reject_reasons


def test_iv_deviation_uses_moneyness_band():
    options_cfg = {
        "moneyness_preference": "ATM_OR_1ITM",
        "min_open_interest": 100,
        "min_volume": 50,
        "max_spread_pct": 0.2,
        "min_option_price": 0.2,
        "max_option_price": 15.0,
        "iv_deviation_pct_max": 50,
        "iv_deviation_hard_reject": True,
        "iv_deviation_band_pct": 1.0,
    }
    service = OptionInstrumentService(options_cfg)
    chain = pd.DataFrame(
        [
            {
                "strike": 90.0,
                "bid": 1.2,
                "ask": 1.25,
                "open_interest": 800,
                "volume": 500,
                "last_price": 1.22,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.8,
            },
            {
                "strike": 100.0,
                "bid": 1.0,
                "ask": 1.18,
                "open_interest": 200,
                "volume": 100,
                "last_price": 1.09,
                "option_type": "CALL",
                "expiration": "2024-01-03",
                "impliedVolatility": 0.2,
            },
        ]
    )

    selection = service.select_contract(
        "GLD",
        ("2024-01-03", chain),
        "CALL",
        100.0,
        "2024-01-02T15:00:00+00:00",
    )

    assert selection.option_contract is not None
    assert selection.option_contract.strike == 100.0
