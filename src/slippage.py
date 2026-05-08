"""Realistic slippage model for retail 1DTE options backtests.

Calibrated against empirical retail-options microstructure research:

- Muravyev & Pearson (2020), "Option Trading Costs Are Lower than You Think"
  RFS 33(11) — sophisticated traders pay ~40% of quoted half-spread, naive
  retail orders pay closer to the full crossing.
- Bryzgalova, Pavlova, Sikorskaya (2023), "Retail Trading in Options and the
  Rise of the Big Three Wholesalers" JF 78(6) — average quoted spread on
  retail-traded weeklies is 12.6% of mid; price improvement is modest and
  highly dispersed.
- Bogousslavsky & Muravyev (2025), "An Anatomy of Retail Option Trading" —
  retail pays 40-60% of quoted spread on average after PFOF improvement.

Components modelled:
- Spread crossing (κ × half_spread), with multipliers for time-of-day
  (L-shape with close re-widen for 1DTE), per-symbol liquidity, intent
  (stop / time-stop), and event windows.
- Latency cost from underlying drift × delta + ½ gamma × (drift)² over the
  decision-to-fill window, plus a quote-staleness penalty.
- Theta bleed during the fill window (matters only near close).
- Size impact for retail-scale orders relative to displayed NBBO size.
- Marketable-limit fill probability (30-70% at mid, ~85% at mid+tick).
- Random noise calibrated as a fraction of half-spread.

The model is parameterised so each component can be calibrated independently
once paper-trading or small-live fills are collected.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import time
from typing import Optional


_DEFAULT_KAPPA_BY_SYMBOL: dict[str, float] = {
    "SPY": 0.45,
    "QQQ": 0.50,
    "GLD": 0.65,
    "SLV": 0.70,
    "NVDA": 0.60,
    "AMZN": 0.60,
}


@dataclass(frozen=True)
class SlippageParams:
    """Parameters for SlippageModel. All independently calibratable."""

    kappa_base: float = 0.55
    kappa_by_symbol: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_KAPPA_BY_SYMBOL)
    )

    # Intraday multipliers on kappa (L-shape, J-tail for 1DTE).
    open_mult: float = 1.8       # 09:30 - 09:35 ET
    early_mult: float = 1.25     # 09:35 - 10:00 ET
    mid_mult: float = 1.0        # 10:00 - 15:30 ET
    close_mult: float = 1.4      # 15:30 - 15:55 ET
    last5_mult: float = 1.8      # 15:55 - 16:00 ET

    # Intent kickers.
    stop_kappa_kick: float = 0.35     # added (not multiplied) for stops
    time_stop_mult: float = 1.15      # multiplier for forced exits
    event_window_mult: float = 1.8    # CPI, FOMC, earnings prints

    # Latency.
    quote_age_penalty_ms: int = 1500  # threshold above which staleness adds drift

    # Marketable-limit fill probabilities.
    ml_fill_prob_at_mid: float = 0.45
    ml_fill_prob_at_mid_plus_tick: float = 0.85

    # Random noise on top of deterministic shift.
    noise_sigma_frac_of_half_spread: float = 0.20

    # Tick / clamp.
    tick_size: float = 0.01


@dataclass(frozen=True)
class FillRequest:
    """Inputs needed to estimate one option fill."""

    side: str                  # "BUY" | "SELL"
    intent: str                # "entry" | "tp" | "stop" | "time_stop"
    bid: float
    ask: float
    underlying_sigma_ann: float
    delta: float
    gamma: float
    theta_per_day: float       # dollars/day, negative for long options
    underlying_price: float
    quote_age_ms: int
    decision_to_submit_ms: int
    submit_to_fill_ms: int
    now_local_time: time       # ET wall clock at decision
    symbol: str
    qty: int
    displayed_size: int
    order_type: str = "marketable_limit_at_mid_plus_tick"
    is_event_window: bool = False


@dataclass(frozen=True)
class FillResult:
    """Output of estimate_fill: either a price or a rejection reason."""

    fill_price: Optional[float]
    status: str                # "filled" | "unfilled_timeout" | "no_quote"


class SlippageModel:
    """Stateful only via its rng. All scenario inputs flow through estimate_fill."""

    def __init__(
        self,
        params: SlippageParams | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.params = params or SlippageParams()
        self.rng = rng or random.Random()

    def estimate_fill(self, req: FillRequest) -> FillResult:
        p = self.params

        if req.bid is None or req.ask is None or req.ask <= req.bid:
            return FillResult(None, "no_quote")

        mid = 0.5 * (req.bid + req.ask)
        half = 0.5 * (req.ask - req.bid)
        sign = +1 if req.side.upper() == "BUY" else -1

        kappa = p.kappa_by_symbol.get(req.symbol, p.kappa_base)
        kappa *= self._intraday_mult(req.now_local_time)
        if req.intent == "stop":
            kappa += p.stop_kappa_kick
        elif req.intent == "time_stop":
            kappa *= p.time_stop_mult
        if req.is_event_window:
            kappa *= p.event_window_mult

        eff_ms = (
            req.decision_to_submit_ms
            + req.submit_to_fill_ms
            + max(0, req.quote_age_ms - p.quote_age_penalty_ms)
        )
        dt_years = eff_ms / 1000.0 / 60.0 / 60.0 / 24.0 / 252.0
        sigma_dS = (
            req.underlying_sigma_ann
            * req.underlying_price
            * math.sqrt(max(dt_years, 1e-12))
        )
        latency_cost = abs(req.delta) * sigma_dS * 0.6 + 0.5 * req.gamma * sigma_dS ** 2

        theta_cost = 0.0
        if req.intent == "time_stop":
            # Trading day = 23,400 seconds (6.5h). Bleed only the elapsed fraction.
            theta_cost = abs(req.theta_per_day) * (eff_ms / 1000.0 / 23400.0)

        impact = 0.0
        if req.displayed_size and req.qty:
            impact = 0.25 * half * math.sqrt(
                max(req.qty, 1) / max(req.displayed_size, 1)
            )

        order_type = req.order_type.lower()
        if order_type == "marketable_limit_at_mid":
            if self.rng.random() > p.ml_fill_prob_at_mid:
                return FillResult(None, "unfilled_timeout")
            spread_share = kappa * half * 0.5
        elif order_type == "marketable_limit_at_mid_plus_tick":
            if self.rng.random() > p.ml_fill_prob_at_mid_plus_tick:
                return FillResult(None, "unfilled_timeout")
            spread_share = kappa * half * 0.85
        else:
            spread_share = kappa * half

        deterministic = sign * (spread_share + latency_cost + impact) + theta_cost
        noise = self.rng.gauss(0.0, p.noise_sigma_frac_of_half_spread * half)
        fill = mid + deterministic + noise

        tick = p.tick_size
        if req.intent == "stop" and req.is_event_window:
            if req.side.upper() == "SELL":
                return FillResult(max(req.bid - 5 * tick, fill), "filled")
            return FillResult(min(req.ask + 5 * tick, fill), "filled")
        clamped = max(req.bid - tick, min(req.ask + tick, fill))
        return FillResult(clamped, "filled")

    def _intraday_mult(self, t: time) -> float:
        p = self.params
        if t < time(9, 35):
            return p.open_mult
        if t < time(10, 0):
            return p.early_mult
        if t < time(15, 30):
            return p.mid_mult
        if t < time(15, 55):
            return p.close_mult
        return p.last5_mult


def effective_over_quoted(fill_price: float, mid: float, half_spread: float, side: str) -> float:
    """EFQ ratio: how much of the half-spread the fill paid (signed by side).

    0.0 = mid; 1.0 = full spread crossed; >1.0 = beyond the quote.
    Used for calibrating kappa from real fills.
    """
    if half_spread <= 0:
        return 0.0
    sign = +1 if side.upper() == "BUY" else -1
    return sign * (fill_price - mid) / half_spread
