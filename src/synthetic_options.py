"""Synthetic option pricer for backtesting when real chain data is unavailable.

Combines:
- Black-Scholes pricing + greeks (call/put), with a graceful intrinsic-only
  fallback when T or IV degenerates.
- Tiered spread proxy by option-price tier (Bryzgalova et al 2023 anchor:
  retail-traded weeklies average ~12.6% of mid quoted spread).
- Realized-vol-based IV proxy with a configurable VRP multiplier.

Synthetic backtests using these primitives must be evaluated with a
~10-20% haircut on the resulting equity curve before accepting "edge,"
because:
- Realized-vol IV proxy systematically misses skew and vol risk premium.
- Tiered spread model misses event-driven blowouts.
- Liquidity blindness on far-OTM strikes.

If edge survives the haircut, real chain data is worth paying for to
confirm. If it doesn't, no data quality upgrade will rescue it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal


_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


@dataclass(frozen=True)
class OptionGreeks:
    """Result of black_scholes(). Theta is per-day (calendar)."""

    price: float
    delta: float
    gamma: float
    theta: float
    vega: float


def black_scholes(
    *,
    underlying: float,
    strike: float,
    time_to_expiry_years: float,
    iv: float,
    risk_free_rate: float = 0.04,
    option_type: Literal["CALL", "PUT"] = "CALL",
) -> OptionGreeks:
    """Black-Scholes price + greeks for a European-style option.

    Inputs:
      underlying: spot price S
      strike: K
      time_to_expiry_years: T in years (e.g., 1/365 for 1DTE held overnight)
      iv: implied volatility, annualized as a fraction (0.20 = 20%)
      risk_free_rate: continuously compounded, annualized
      option_type: "CALL" or "PUT"

    Theta is normalized to per-day decay. Gamma and vega use standard BS
    units (per $1 underlying move; per 100% IV move respectively — vega
    is reported as price-per-1.0-IV; divide by 100 for per-1%-IV).
    """
    if time_to_expiry_years <= 0 or iv <= 0 or underlying <= 0 or strike <= 0:
        if option_type == "CALL":
            intrinsic = max(underlying - strike, 0.0)
            delta = 1.0 if underlying > strike else 0.0
        else:
            intrinsic = max(strike - underlying, 0.0)
            delta = -1.0 if underlying < strike else 0.0
        return OptionGreeks(price=intrinsic, delta=delta, gamma=0.0, theta=0.0, vega=0.0)

    sigma = iv
    T = time_to_expiry_years
    sqrtT = math.sqrt(T)
    d1 = (math.log(underlying / strike) + (risk_free_rate + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    discount = math.exp(-risk_free_rate * T)
    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)

    if option_type == "CALL":
        price = underlying * cdf_d1 - strike * discount * cdf_d2
        delta = cdf_d1
        theta_per_year = (
            -(underlying * pdf_d1 * sigma) / (2.0 * sqrtT)
            - risk_free_rate * strike * discount * cdf_d2
        )
    else:
        cdf_neg_d1 = _norm_cdf(-d1)
        cdf_neg_d2 = _norm_cdf(-d2)
        price = strike * discount * cdf_neg_d2 - underlying * cdf_neg_d1
        delta = cdf_d1 - 1.0
        theta_per_year = (
            -(underlying * pdf_d1 * sigma) / (2.0 * sqrtT)
            + risk_free_rate * strike * discount * cdf_neg_d2
        )

    gamma = pdf_d1 / (underlying * sigma * sqrtT)
    vega = underlying * pdf_d1 * sqrtT
    theta = theta_per_year / 365.0

    return OptionGreeks(price=price, delta=delta, gamma=gamma, theta=theta, vega=vega)


@dataclass(frozen=True)
class SpreadParams:
    """Tiered synthetic spread by option-price tier (fraction of mid).

    Anchor: Bryzgalova et al (2023) report retail weeklies average ~12.6%
    quoted spread of mid. Lower-priced strikes pay more in percent terms;
    higher-priced strikes pay less. Tighten after collecting real fills.
    """

    tiers: tuple[tuple[float, float], ...] = (
        (0.50, 0.20),
        (1.00, 0.15),
        (2.00, 0.12),
        (5.00, 0.07),
        (15.00, 0.05),
        (float("inf"), 0.04),
    )


def estimate_spread_pct(option_mid: float, params: SpreadParams | None = None) -> float:
    """Spread as a fraction of mid for the given option price tier."""
    p = params or SpreadParams()
    for max_price, spread_pct in p.tiers:
        if option_mid <= max_price:
            return spread_pct
    return p.tiers[-1][1]


def synthetic_bid_ask(
    option_mid: float,
    params: SpreadParams | None = None,
) -> tuple[float, float]:
    """Construct synthetic (bid, ask) centered on mid for the price tier."""
    if option_mid <= 0:
        return 0.0, 0.0
    spread_pct = estimate_spread_pct(option_mid, params)
    half = 0.5 * option_mid * spread_pct
    bid = max(option_mid - half, 0.01)
    ask = option_mid + half
    return bid, ask


@dataclass(frozen=True)
class IVProxyParams:
    """Realized-vol-based IV proxy parameters.

    Caller provides log returns at any frequency along with the matching
    annualization factor (e.g. 252 for daily, 252*390 for 1-min, 252*78 for
    5-min). The result is annualized vol scaled by vrp_multiplier and clamped
    to [floor, ceiling].
    """

    bars_per_year: float = 252.0  # default = daily returns
    vrp_multiplier: float = 1.20
    floor: float = 0.10
    ceiling: float = 2.00


def realized_vol_iv_proxy(
    log_returns: Iterable[float],
    params: IVProxyParams | None = None,
) -> float:
    """Annualized IV estimate from a window of log returns."""
    p = params or IVProxyParams()
    returns = list(log_returns)
    if len(returns) < 2:
        return p.floor
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    realized_vol_annualized = math.sqrt(variance * p.bars_per_year)
    iv = realized_vol_annualized * p.vrp_multiplier
    return max(p.floor, min(p.ceiling, iv))
