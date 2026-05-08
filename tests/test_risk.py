import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.models import OptionContract, OptionGreeks
from src.risk import RiskEngine, calculate_position_size


def _make_option() -> OptionContract:
    return OptionContract(
        symbol="GLD",
        expiration="2024-01-03",
        strike=100.0,
        option_type="CALL",
        bid=1.0,
        ask=1.1,
        mid=1.05,
        implied_volatility=0.25,
        spread=0.1,
        spread_pct=0.095,
        nbbo_bid=1.0,
        nbbo_ask=1.1,
        open_interest=500,
        volume=200,
        last_price=1.05,
        underlying_price=100.0,
        time_to_expiry_days=1.0,
        quote_time_utc="2024-01-02T15:00:00+00:00",
        greeks=OptionGreeks(),
    )


def test_risk_sizing_caps():
    result = calculate_position_size(
        account_equity=10000,
        risk_pct=0.1,
        option_mid=1.0,
        premium_stop_pct=0.25,
        max_premium_per_trade=1200,
        max_contracts=15,
    )
    assert result["contracts"] == 12
    assert math.isclose(result["estimated_risk"], 12 * 25.0)
    assert math.isclose(result["estimated_premium"], 12 * 100.0)


def test_risk_sizing_delta_atr():
    result = calculate_position_size(
        account_equity=10000,
        risk_pct=0.1,
        option_mid=1.0,
        premium_stop_pct=0.25,
        max_premium_per_trade=2000,
        max_contracts=20,
        stop_mode="delta_atr",
        atr_value=1.5,
        delta=0.5,
        atr_stop_multiplier=2.0,
    )
    expected_risk_per_contract = 0.5 * 1.5 * 100.0 * 2.0
    expected_contracts = math.floor((10000 * 0.1) / expected_risk_per_contract)
    assert result["contracts"] == expected_contracts
    assert math.isclose(result["estimated_risk"], expected_contracts * expected_risk_per_contract)


def test_risk_invalid_mid():
    result = calculate_position_size(
        account_equity=10000,
        risk_pct=0.1,
        option_mid=0.0,
        premium_stop_pct=0.25,
        max_premium_per_trade=1200,
        max_contracts=15,
    )
    assert result["contracts"] == 0


def test_risk_engine_kill_switch():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "risk_controls": {"kill_switch": True},
    }
    decision = engine.assess("GLD", option, "2024-01-02T15:00:00+00:00", "CALL", config)
    assert decision.allowed is False
    assert "Kill switch active" in decision.reject_reasons


def test_risk_engine_throttle():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "risk_controls": {"min_seconds_between_signals": 300},
    }
    decision_one = engine.assess("GLD", option, "2024-01-02T15:00:00+00:00", "CALL", config)
    decision_two = engine.assess("GLD", option, "2024-01-02T15:01:00+00:00", "CALL", config)
    assert decision_one.allowed is True
    assert decision_two.allowed is False
    assert "Signal throttle" in " ".join(decision_two.reject_reasons)


def test_duplicate_order_detection():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "risk_controls": {"duplicate_order_window_seconds": 300},
    }
    decision_one = engine.assess("GLD", option, "2024-01-02T15:00:00+00:00", "CALL", config)
    decision_two = engine.assess("GLD", option, "2024-01-02T15:03:00+00:00", "CALL", config)
    assert decision_one.allowed is True
    assert decision_two.allowed is False
    assert "Duplicate order" in " ".join(decision_two.reject_reasons)


def test_max_position_per_symbol():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "risk_controls": {"max_position_per_symbol": 20},
    }
    decision_one = engine.assess("GLD", option, "2024-01-02T15:00:00+00:00", "CALL", config)
    decision_two = engine.assess("GLD", option, "2024-01-02T15:10:00+00:00", "CALL", config)
    assert decision_one.allowed is True
    assert decision_two.allowed is False
    assert "Max position" in " ".join(decision_two.reject_reasons)


def test_daily_loss_lockout_and_cooldown():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {
            "equity": 10000,
            "risk_pct": 0.1,
            "max_daily_loss_pct": 0.02,
            "cooldown_minutes_after_loss": 30,
        },
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "runtime": {
            "daily_state": {
                "realized_pnl": -250.0,
                "last_loss_time_utc": "2024-01-02T15:10:00+00:00",
            }
        },
        "risk_controls": {},
    }
    decision = engine.assess("GLD", option, "2024-01-02T15:20:00+00:00", "CALL", config)
    assert decision.allowed is False
    assert "Daily loss" in " ".join(decision.reject_reasons)
    assert "Cooldown" in " ".join(decision.reject_reasons)


def test_volatility_adjustment_applies():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
            "volatility_adjustment": {
                "enabled": True,
                "atr_target_pct": 1.0,
                "min_risk_pct": 0.01,
                "max_risk_pct": 0.1,
            },
        },
        "risk_controls": {},
    }
    decision = engine.assess(
        "GLD",
        option,
        "2024-01-02T15:00:00+00:00",
        "CALL",
        config,
        atr_pct=2.0,
    )
    assert decision.allowed is True
    assert math.isclose(decision.risk_pct_used, 0.05)


def test_data_health_block():
    engine = RiskEngine()
    option = _make_option()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "risk_controls": {"block_on_data_error": True},
        "runtime": {"data_health_ok": False},
    }
    decision = engine.assess("GLD", option, "2024-01-02T15:20:00+00:00", "CALL", config)
    assert decision.allowed is False
    assert "Data provider unhealthy" in " ".join(decision.reject_reasons)


def test_event_risk_block():
    engine = RiskEngine()
    option = _make_option()
    # Engine compares against ET-local date, so the test's "today" must
    # be in ET too — otherwise the test fails when run during the UTC
    # window that's already past midnight while ET hasn't rolled over yet
    # (roughly 8pm-midnight ET / 0:00-4:00 UTC).
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "risk_controls": {
            "event_risk_dates": [today],
            "control_levels": {"event_risk": "hard"},
        },
    }
    decision = engine.assess("GLD", option, f"{today}T15:20:00+00:00", "CALL", config)
    assert decision.allowed is False
    assert "Event risk date" in " ".join(decision.reject_reasons)
