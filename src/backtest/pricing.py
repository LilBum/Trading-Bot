"""Backtest-specific option pricing helpers.

Wraps synthetic_options for the runner's needs:
- time-to-expiry math for 1DTE held overnight,
- ATM strike rounding,
- pricing an existing position at current underlying.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.synthetic_options import OptionGreeks, black_scholes


EASTERN = ZoneInfo("America/New_York")
SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def time_to_next_close_years(current_time_et: datetime) -> float:
    """Calendar-year fraction from `current_time_et` to tomorrow 16:00 ET."""
    if current_time_et.tzinfo is None:
        current_time_et = current_time_et.replace(tzinfo=EASTERN)
    tomorrow = current_time_et + timedelta(days=1)
    tomorrow_close = tomorrow.replace(hour=16, minute=0, second=0, microsecond=0)
    seconds = max(0.0, (tomorrow_close - current_time_et).total_seconds())
    return seconds / SECONDS_PER_YEAR


def time_to_target_close_years(current_time_et: datetime, expiration_date: str) -> float:
    """Calendar-year fraction from `current_time_et` to 16:00 ET on `expiration_date` (YYYY-MM-DD)."""
    if current_time_et.tzinfo is None:
        current_time_et = current_time_et.replace(tzinfo=EASTERN)
    year, month, day = (int(part) for part in expiration_date.split("-"))
    target = datetime(year, month, day, 16, 0, tzinfo=EASTERN)
    seconds = max(0.0, (target - current_time_et).total_seconds())
    return seconds / SECONDS_PER_YEAR


def round_to_strike(underlying_price: float, increment: float = 1.0) -> float:
    """Round to the nearest standard strike. Default = $1 increments."""
    if increment <= 0:
        raise ValueError("increment must be > 0")
    return round(underlying_price / increment) * increment


def next_session_date(current_time_et: datetime) -> str:
    """ISO date of the next ET calendar day. Caller may need a holiday-adjusted variant later."""
    if current_time_et.tzinfo is None:
        current_time_et = current_time_et.replace(tzinfo=EASTERN)
    return (current_time_et + timedelta(days=1)).date().isoformat()


def price_atm_option(
    *,
    underlying: float,
    direction: str,
    time_to_expiry_years: float,
    iv: float,
    risk_free_rate: float = 0.04,
    strike_increment: float = 1.0,
) -> tuple[float, OptionGreeks]:
    """Pick the nearest ATM strike for `direction` and price it."""
    strike = round_to_strike(underlying, strike_increment)
    option_type = "CALL" if direction.upper() == "CALL" else "PUT"
    greeks = black_scholes(
        underlying=underlying,
        strike=strike,
        time_to_expiry_years=time_to_expiry_years,
        iv=iv,
        risk_free_rate=risk_free_rate,
        option_type=option_type,
    )
    return strike, greeks


def price_existing_option(
    *,
    underlying: float,
    strike: float,
    direction: str,
    time_to_expiry_years: float,
    iv: float,
    risk_free_rate: float = 0.04,
) -> OptionGreeks:
    """Reprice an existing position at the current underlying."""
    option_type = "CALL" if direction.upper() == "CALL" else "PUT"
    return black_scholes(
        underlying=underlying,
        strike=strike,
        time_to_expiry_years=time_to_expiry_years,
        iv=iv,
        risk_free_rate=risk_free_rate,
        option_type=option_type,
    )
