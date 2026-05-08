from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.planner import build_trade_plan


def _make_intraday_df(start_time: datetime, minutes: int = 60) -> pd.DataFrame:
    timestamps = pd.date_range(start_time, periods=minutes, freq="1min", tz=start_time.tzinfo)
    prices = [100 + (idx * 0.1) for idx in range(minutes)]
    frame = pd.DataFrame(
        {
            "Open": prices,
            "High": prices,
            "Low": prices,
            "Close": prices,
            "Volume": [1000] * minutes,
        },
        index=timestamps,
    )
    return frame


def test_build_trade_plan_rejects_without_chain():
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "strategy": {
            "vwap_slope_lookback": 5,
            "ema_fast": 3,
            "ema_slow": 5,
            "pullback_lookback": 5,
            "pullback_vwap_tolerance_pct": 5.0,
            "momentum_min_pct": 0.01,
            "chop_lookback_minutes": 10,
            "max_vwap_crosses": 4,
            "time_filters": {
                "avoid_open_minutes": 10,
                "avoid_close_minutes": 60,
                "scalp_mode": False,
            },
        },
        "options": {
            "target_dte": 1,
            "moneyness_preference": "ATM_OR_1ITM",
            "min_open_interest": 300,
            "min_volume": 150,
            "max_spread_pct": 0.08,
            "min_option_price": 0.2,
            "max_option_price": 15.0,
        },
    }
    df = _make_intraday_df(datetime(2024, 1, 2, 11, 0, tzinfo=ZoneInfo("America/New_York")))
    plan = build_trade_plan("GLD", df, None, config)
    assert plan.status == "REJECTED"
    assert "Options chain data unavailable" in plan.reject_reasons


def test_build_trade_plan_allows_with_chain():
    config = {
        "account": {"equity": 10000, "risk_pct": 0.1},
        "position_sizing": {
            "premium_stop_pct": 0.25,
            "max_premium_per_trade": 1200,
            "max_contracts": 15,
        },
        "strategy": {
            "vwap_slope_lookback": 5,
            "ema_fast": 3,
            "ema_slow": 5,
            "pullback_lookback": 5,
            "pullback_vwap_tolerance_pct": 5.0,
            "momentum_min_pct": 0.01,
            "chop_lookback_minutes": 10,
            "max_vwap_crosses": 6,
            "time_filters": {
                "avoid_open_minutes": 10,
                "avoid_close_minutes": 60,
                "scalp_mode": False,
            },
        },
        "options": {
            "target_dte": 1,
            "moneyness_preference": "ATM_OR_1ITM",
            "min_open_interest": 100,
            "min_volume": 50,
            "max_spread_pct": 0.2,
            "min_option_price": 0.2,
            "max_option_price": 15.0,
        },
    }
    df = _make_intraday_df(datetime(2024, 1, 2, 11, 0, tzinfo=ZoneInfo("America/New_York")))
    chain = pd.DataFrame(
        [
            {
                "strike": 105.0,
                "bid": 1.2,
                "ask": 1.3,
                "open_interest": 500,
                "volume": 200,
                "last_price": 1.25,
                "option_type": "CALL",
                "expiration": "2024-01-03",
            }
        ]
    )
    plan = build_trade_plan("GLD", df, ("2024-01-03", chain), config)
    assert plan.status == "ALLOWED"
    assert plan.contracts > 0
