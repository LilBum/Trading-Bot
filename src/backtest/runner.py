"""Per-session bar-by-bar backtest simulator.

Runs the existing VwapPullbackSignalEngine across one TradingSession,
opens at most one synthetic 1DTE option position per session, and exits
on TP / SL / time-stop / session-close-buffer with slippage-realistic
fills. Returns a list of ClosedTrade records ready for metrics.

The signal engine is fed a sliced view of the bars (only data up to and
including the current bar) so there is no look-ahead. Stale-bar
rejection is bypassed by setting `data_quality.max_bar_age_minutes` to
None in the runtime config — the wall-clock check inside the engine is
inappropriate for historical replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from src.backtest.positions import (
    ClosedTrade,
    OpenPosition,
    close_position,
    evaluate_exit,
)
from src.backtest.pricing import (
    next_session_date,
    price_atm_option,
    price_existing_option,
    time_to_target_close_years,
)
from src.backtest.sessions import TradingSession
from src.engines.signal_engine import VwapPullbackSignalEngine
from src.slippage import FillRequest, SlippageModel
from src.synthetic_options import SpreadParams, synthetic_bid_ask


@dataclass
class RunnerConfig:
    """Inputs to SessionRunner. Mirrors structure of config.json strategy/exits."""

    strategy_cfg: dict
    exits_cfg: dict
    iv_by_symbol: dict[str, float] = field(
        default_factory=lambda: {
            "SPY": 0.18, "QQQ": 0.22, "GLD": 0.16, "SLV": 0.30,
            "NVDA": 0.50, "AMZN": 0.40,
        }
    )
    strike_increment_by_symbol: dict[str, float] = field(
        default_factory=lambda: {
            "SPY": 1.0, "QQQ": 1.0, "GLD": 1.0, "SLV": 0.5,
            "NVDA": 1.0, "AMZN": 1.0,
        }
    )
    contracts_per_trade: int = 1
    contract_multiplier: int = 100
    risk_free_rate: float = 0.04

    quote_age_ms: int = 200
    decision_to_submit_ms: int = 300
    submit_to_fill_ms: int = 200
    displayed_size: int = 50

    min_signal_bars: int = 30
    spread_params: Optional[SpreadParams] = None
    underlying_sigma_for_slippage_by_symbol: dict[str, float] = field(
        default_factory=lambda: {
            "SPY": 0.18, "QQQ": 0.22, "GLD": 0.16, "SLV": 0.30,
            "NVDA": 0.50, "AMZN": 0.40,
        }
    )


@dataclass
class SessionResult:
    """Outcome of one session backtest."""

    symbol: str
    session_date: str
    trades: list[ClosedTrade]


class SessionRunner:
    def __init__(
        self,
        runner_cfg: RunnerConfig,
        slippage_model: SlippageModel,
        signal_engine: object | None = None,
    ) -> None:
        self.cfg = runner_cfg
        self.slippage_model = slippage_model
        self.signal_engine = signal_engine or VwapPullbackSignalEngine(runner_cfg.strategy_cfg)

    def run_session(self, symbol: str, session: TradingSession) -> SessionResult:
        bars = session.bars
        if len(bars) < self.cfg.min_signal_bars:
            return SessionResult(symbol=symbol, session_date=session.session_date, trades=[])

        trades: list[ClosedTrade] = []
        open_position: OpenPosition | None = None
        entered_today = False

        runtime_config = {
            "data_quality": {
                "max_bar_age_minutes": None,
                "allow_stale_when_closed": True,
            },
            "position_sizing": {
                "stop_mode": "premium_pct",
                "premium_stop_pct": float(self.cfg.exits_cfg.get("stop_loss_pct", 0.25)),
            },
            "sentiment": {"enabled": False},
        }
        iv = float(self.cfg.iv_by_symbol.get(symbol, 0.20))
        sigma_ann = float(self.cfg.underlying_sigma_for_slippage_by_symbol.get(symbol, iv))
        strike_increment = float(self.cfg.strike_increment_by_symbol.get(symbol, 1.0))
        session_close_et = bars.index[0].replace(
            hour=16, minute=0, second=0, microsecond=0
        ).to_pydatetime()

        for i in range(self.cfg.min_signal_bars - 1, len(bars)):
            bar_view = bars.iloc[: i + 1]
            current_time_et = bar_view.index[-1].to_pydatetime()
            current_close = float(bar_view["Close"].iloc[-1])
            minutes_to_close = max(
                0.0, (session_close_et - current_time_et).total_seconds() / 60.0
            )

            if open_position is not None:
                trade = self._maybe_exit(
                    open_position, symbol, sigma_ann, current_close,
                    current_time_et, minutes_to_close,
                )
                if trade is not None:
                    trades.append(trade)
                    open_position = None

            if open_position is None and not entered_today:
                opened = self._maybe_enter(
                    symbol, bar_view, current_close, current_time_et,
                    iv, sigma_ann, strike_increment, runtime_config,
                )
                if opened is not None:
                    open_position = opened
                    entered_today = True

        if open_position is not None:
            forced = self._force_close(open_position, symbol, sigma_ann, bars)
            if forced is not None:
                trades.append(forced)

        return SessionResult(
            symbol=symbol,
            session_date=session.session_date,
            trades=trades,
        )

    def _maybe_enter(
        self,
        symbol: str,
        bar_view: pd.DataFrame,
        current_close: float,
        current_time_et: datetime,
        iv: float,
        sigma_ann: float,
        strike_increment: float,
        runtime_config: dict,
    ) -> OpenPosition | None:
        try:
            signal = self.signal_engine.evaluate(symbol, bar_view, runtime_config)
        except Exception:
            return None
        if signal.direction not in ("CALL", "PUT") or signal.reject_reasons:
            return None

        expiration = next_session_date(current_time_et)
        T = time_to_target_close_years(current_time_et, expiration)
        strike, theo = price_atm_option(
            underlying=current_close,
            direction=signal.direction,
            time_to_expiry_years=T,
            iv=iv,
            risk_free_rate=self.cfg.risk_free_rate,
            strike_increment=strike_increment,
        )
        if theo.price <= 0.0:
            return None

        bid, ask = synthetic_bid_ask(theo.price, params=self.cfg.spread_params)
        fill = self.slippage_model.estimate_fill(
            FillRequest(
                side="BUY",
                intent="entry",
                bid=bid,
                ask=ask,
                underlying_sigma_ann=sigma_ann,
                delta=theo.delta,
                gamma=theo.gamma,
                theta_per_day=theo.theta,
                underlying_price=current_close,
                quote_age_ms=self.cfg.quote_age_ms,
                decision_to_submit_ms=self.cfg.decision_to_submit_ms,
                submit_to_fill_ms=self.cfg.submit_to_fill_ms,
                now_local_time=current_time_et.time(),
                symbol=symbol,
                qty=self.cfg.contracts_per_trade,
                displayed_size=self.cfg.displayed_size,
                order_type="marketable_limit_at_mid_plus_tick",
            )
        )
        if fill.fill_price is None:
            return None

        return OpenPosition(
            symbol=symbol,
            direction=signal.direction,
            strike=strike,
            expiration_date=expiration,
            contracts=self.cfg.contracts_per_trade,
            entry_time_et=current_time_et,
            entry_price=fill.fill_price,
            entry_underlying=current_close,
            entry_iv=iv,
            entry_delta=theo.delta,
            entry_gamma=theo.gamma,
            entry_theta_per_day=theo.theta,
            take_profit_pct=float(self.cfg.exits_cfg.get("take_profit_pct", 0.30)),
            stop_loss_pct=float(self.cfg.exits_cfg.get("stop_loss_pct", 0.25)),
            max_hold_minutes=int(self.cfg.exits_cfg.get("max_hold_minutes", 120)),
            contract_multiplier=self.cfg.contract_multiplier,
        )

    def _maybe_exit(
        self,
        position: OpenPosition,
        symbol: str,
        sigma_ann: float,
        current_close: float,
        current_time_et: datetime,
        minutes_to_close: float,
    ) -> ClosedTrade | None:
        T = time_to_target_close_years(current_time_et, position.expiration_date)
        cur_greeks = price_existing_option(
            underlying=current_close,
            strike=position.strike,
            direction=position.direction,
            time_to_expiry_years=T,
            iv=position.entry_iv,
            risk_free_rate=self.cfg.risk_free_rate,
        )
        exit_reason = evaluate_exit(
            position,
            current_option_price=cur_greeks.price,
            current_time_et=current_time_et,
            minutes_to_session_close=minutes_to_close,
            exit_before_close_minutes=float(
                self.cfg.exits_cfg.get("exit_before_close_minutes", 5)
            ),
        )
        if exit_reason is None:
            return None

        intent = "stop" if exit_reason == "stop" else (
            "tp" if exit_reason == "tp" else "time_stop"
        )
        order_type = "market" if exit_reason == "stop" else "marketable_limit_at_mid_plus_tick"
        bid, ask = synthetic_bid_ask(cur_greeks.price, params=self.cfg.spread_params)
        fill = self.slippage_model.estimate_fill(
            FillRequest(
                side="SELL",
                intent=intent,
                bid=bid,
                ask=ask,
                underlying_sigma_ann=sigma_ann,
                delta=cur_greeks.delta,
                gamma=cur_greeks.gamma,
                theta_per_day=cur_greeks.theta,
                underlying_price=current_close,
                quote_age_ms=self.cfg.quote_age_ms,
                decision_to_submit_ms=self.cfg.decision_to_submit_ms,
                submit_to_fill_ms=self.cfg.submit_to_fill_ms,
                now_local_time=current_time_et.time(),
                symbol=symbol,
                qty=position.contracts,
                displayed_size=self.cfg.displayed_size,
                order_type=order_type,
            )
        )
        if fill.fill_price is None:
            return None

        return close_position(
            position,
            exit_time_et=current_time_et,
            exit_price=fill.fill_price,
            exit_reason=exit_reason,
        )

    def _force_close(
        self,
        position: OpenPosition,
        symbol: str,
        sigma_ann: float,
        bars: pd.DataFrame,
    ) -> ClosedTrade | None:
        last_bar_time = bars.index[-1].to_pydatetime()
        last_close = float(bars["Close"].iloc[-1])
        T = time_to_target_close_years(last_bar_time, position.expiration_date)
        final_greeks = price_existing_option(
            underlying=last_close,
            strike=position.strike,
            direction=position.direction,
            time_to_expiry_years=T,
            iv=position.entry_iv,
            risk_free_rate=self.cfg.risk_free_rate,
        )
        bid, ask = synthetic_bid_ask(final_greeks.price, params=self.cfg.spread_params)
        fill = self.slippage_model.estimate_fill(
            FillRequest(
                side="SELL",
                intent="time_stop",
                bid=bid,
                ask=ask,
                underlying_sigma_ann=sigma_ann,
                delta=final_greeks.delta,
                gamma=final_greeks.gamma,
                theta_per_day=final_greeks.theta,
                underlying_price=last_close,
                quote_age_ms=self.cfg.quote_age_ms,
                decision_to_submit_ms=self.cfg.decision_to_submit_ms,
                submit_to_fill_ms=self.cfg.submit_to_fill_ms,
                now_local_time=last_bar_time.time(),
                symbol=symbol,
                qty=position.contracts,
                displayed_size=self.cfg.displayed_size,
                order_type="market",
            )
        )
        # Last-resort fill: even if the slippage model bails, we mark to model.
        exit_price = fill.fill_price if fill.fill_price is not None else final_greeks.price
        return close_position(
            position,
            exit_time_et=last_bar_time,
            exit_price=exit_price,
            exit_reason="session_close",
        )
