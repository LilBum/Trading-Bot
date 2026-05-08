"""Slippage model for E-mini futures (ES, NQ, YM, RTY).

Cost structure is fundamentally different from options:
- Spreads are fixed in ticks, usually 1 tick wide for major e-minis (ES/NQ).
- No theta, no early exercise, no premium decay.
- Latency cost is just underlying drift over the fill window.
- Stops cross more aggressively than entries (sweep liquidity).
- Fills round to tick.

Calibrated against published microstructure research on CME e-mini liquidity
(Hasbrouck, Easley/O'Hara) and practitioner reports of typical retail fills.
Tighter and simpler than the options slippage model.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import time
from typing import Optional


@dataclass(frozen=True)
class FuturesContract:
    """Spec for one futures contract used by the slippage and PnL math."""

    tick_size: float       # price increment in points (ES/NQ = 0.25)
    point_value: float     # dollars per point (ES = $50, NQ = $20, YM = $5, RTY = $50)

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value


# Standard CME E-mini contract specs as of 2026.
CONTRACTS: dict[str, FuturesContract] = {
    "ES":  FuturesContract(tick_size=0.25, point_value=50.0),   # E-mini S&P 500
    "NQ":  FuturesContract(tick_size=0.25, point_value=20.0),   # E-mini Nasdaq-100
    "YM":  FuturesContract(tick_size=1.00, point_value=5.0),    # E-mini Dow
    "RTY": FuturesContract(tick_size=0.10, point_value=50.0),   # E-mini Russell 2000
    # Micro variants if we want them later: MES (1/10 ES), MNQ (1/10 NQ), MYM, M2K
    "MES": FuturesContract(tick_size=0.25, point_value=5.0),
    "MNQ": FuturesContract(tick_size=0.25, point_value=2.0),
}


@dataclass(frozen=True)
class FuturesSlippageParams:
    """Parameters for FuturesSlippageModel. All independently calibratable."""

    # Effective-over-quoted half-spread crossed for retail market orders.
    # Empirical: retail pays ~0.5 of the quoted spread on a typical market order
    # in liquid e-minis at midday. Wider on stops; wider at session boundaries.
    kappa_base: float = 0.5

    # Intraday multipliers (much milder than options — futures liquidity is steadier).
    overnight_mult: float = 1.4   # outside RTH (pre-08:00 ET, post-16:00 ET)
    pre_open_mult: float = 1.1    # 08:00 - 09:30 ET
    open_mult: float = 1.5        # 09:30 - 09:35 ET (cash-open spike)
    early_mult: float = 1.1       # 09:35 - 10:00 ET
    mid_mult: float = 1.0         # 10:00 - 15:30 ET
    close_mult: float = 1.2       # 15:30 - 16:00 ET

    # Intent kickers.
    stop_kappa_kick: float = 0.5     # stops sweep multiple ticks of liquidity
    economic_release_mult: float = 1.8  # 08:30 ET data drops, FOMC, etc.

    # Latency / staleness.
    quote_age_penalty_ms: int = 500  # futures top-of-book updates much faster than options

    # Marketable-limit fill probabilities.
    ml_fill_prob_at_mid: float = 0.55
    ml_fill_prob_at_one_tick_through: float = 0.95

    # Random component on top of deterministic shift, in fractions of a tick.
    noise_sigma_frac_of_tick: float = 0.15


@dataclass(frozen=True)
class FuturesFillRequest:
    """Inputs needed to estimate one futures fill."""

    side: str                  # "BUY" | "SELL"
    intent: str                # "entry" | "tp" | "stop" | "time_stop"
    bid: float                 # in price points
    ask: float
    underlying_sigma_ann: float  # annualized vol of the underlying
    quote_age_ms: int
    decision_to_submit_ms: int
    submit_to_fill_ms: int
    now_local_time: time       # ET wall clock at decision
    symbol: str                # "ES", "NQ", etc.
    qty: int
    order_type: str = "market"  # "market" | "marketable_limit_at_mid" | "marketable_limit_at_one_tick_through"
    is_economic_release: bool = False


@dataclass(frozen=True)
class FuturesFillResult:
    fill_price: Optional[float]
    status: str                # "filled" | "unfilled_timeout" | "no_quote"


class FuturesSlippageModel:
    """Stateful only via its rng. Scenario inputs flow through estimate_fill."""

    def __init__(
        self,
        params: FuturesSlippageParams | None = None,
        contracts: dict[str, FuturesContract] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.params = params or FuturesSlippageParams()
        self.contracts = dict(contracts) if contracts is not None else dict(CONTRACTS)
        self.rng = rng or random.Random()

    def estimate_fill(self, req: FuturesFillRequest) -> FuturesFillResult:
        if req.bid is None or req.ask is None or req.ask <= req.bid:
            return FuturesFillResult(None, "no_quote")

        spec = self.contracts.get(req.symbol)
        if spec is None:
            spec = FuturesContract(tick_size=0.01, point_value=1.0)

        mid = 0.5 * (req.bid + req.ask)
        half = 0.5 * (req.ask - req.bid)
        sign = +1 if req.side.upper() == "BUY" else -1
        p = self.params

        kappa = p.kappa_base * self._intraday_mult(req.now_local_time)
        if req.intent == "stop":
            kappa += p.stop_kappa_kick
        if req.is_economic_release:
            kappa *= p.economic_release_mult

        eff_ms = (
            req.decision_to_submit_ms
            + req.submit_to_fill_ms
            + max(0, req.quote_age_ms - p.quote_age_penalty_ms)
        )
        dt_years = eff_ms / 1000.0 / (365.0 * 24.0 * 3600.0)
        sigma_dS = req.underlying_sigma_ann * mid * math.sqrt(max(dt_years, 1e-12))
        latency_cost = sigma_dS  # for futures, full underlying drift hits the fill

        order_type = req.order_type.lower()
        if order_type == "marketable_limit_at_mid":
            if self.rng.random() > p.ml_fill_prob_at_mid:
                return FuturesFillResult(None, "unfilled_timeout")
            spread_share = kappa * half * 0.5
        elif order_type == "marketable_limit_at_one_tick_through":
            if self.rng.random() > p.ml_fill_prob_at_one_tick_through:
                return FuturesFillResult(None, "unfilled_timeout")
            spread_share = kappa * half * 0.85
        else:
            spread_share = kappa * half

        deterministic = sign * (spread_share + latency_cost)
        noise = self.rng.gauss(0.0, p.noise_sigma_frac_of_tick * spec.tick_size)
        # Fill price is the expected average cost across many fills. Per-trade
        # tick rounding would collapse model differentiation (e.g., entry vs
        # stop both round to the ask in a 1-tick-wide e-mini), which corrupts
        # backtest cumulative PnL — leave it continuous.
        fill = mid + deterministic + noise
        return FuturesFillResult(fill_price=fill, status="filled")

    def _intraday_mult(self, t: time) -> float:
        p = self.params
        if t < time(8, 0):
            return p.overnight_mult
        if t < time(9, 30):
            return p.pre_open_mult
        if t < time(9, 35):
            return p.open_mult
        if t < time(10, 0):
            return p.early_mult
        if t < time(15, 30):
            return p.mid_mult
        if t < time(16, 0):
            return p.close_mult
        return p.overnight_mult


def realized_pnl_points(
    entry_price: float,
    exit_price: float,
    side: str,
    contracts: int,
) -> float:
    """PnL in points (price units) for a long or short futures position."""
    direction = +1 if side.upper() == "BUY" else -1
    return direction * (exit_price - entry_price) * contracts


def realized_pnl_dollars(
    entry_price: float,
    exit_price: float,
    side: str,
    contracts: int,
    point_value: float,
) -> float:
    """PnL in dollars: points × point_value × contracts."""
    return realized_pnl_points(entry_price, exit_price, side, contracts) * point_value
