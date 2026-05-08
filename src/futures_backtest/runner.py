"""Per-session bar-by-bar backtest simulator for futures.

Differences from src/backtest/runner.py:
- Linear PnL (no synthetic option pricing).
- BUY/SELL sides (futures can be short).
- Stops/targets in price points, not %-of-premium.
- Synthetic 1-tick spread around current close for slippage modelling.
- Translates ORB-engine CALL/PUT direction into BUY/SELL futures order side.

Same look-ahead-free guarantee as the options runner: signal engine sees only
bars up to and including the current bar. Wall-clock staleness check bypassed
via `data_quality.max_bar_age_minutes=None` in the runtime config we pass to
the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from src.futures_backtest.sessions import FuturesTradingSession
from src.futures_position import (
    FuturesClosedTrade,
    FuturesOpenPosition,
    close_position,
    evaluate_exit,
    evaluate_exit_intrabar,
)
from src.futures_slippage import (
    CONTRACTS,
    FuturesContract,
    FuturesFillRequest,
    FuturesSlippageModel,
)


@dataclass
class FuturesRunnerConfig:
    """Inputs to FuturesSessionRunner."""

    symbol_to_contract: dict[str, FuturesContract] = field(
        default_factory=lambda: dict(CONTRACTS)
    )
    # Defaults set to the V2-winning NQ parameters (the validated edge config).
    # Override per-run when backtesting other symbols / variants.
    # NQ: $20/pt → 50pt stop = $1000 risk, 100pt target = $2000 reward (2:1 R:R).
    take_profit_points: float = 100.0
    stop_loss_points: float = 50.0
    max_hold_minutes: int = 120
    exit_before_close_minutes: int = 5
    contracts_per_trade: int = 1

    underlying_sigma_for_slippage_by_symbol: dict[str, float] = field(
        default_factory=lambda: {"ES": 0.18, "NQ": 0.22}
    )

    quote_age_ms: int = 200
    decision_to_submit_ms: int = 300
    submit_to_fill_ms: int = 200

    # ORB needs ~16 bars (15-min opening range + 1 confirmation) before signalling.
    min_signal_bars: int = 16

    # When True, TP/SL fire on intrabar high/low (matches broker-side OCO
    # behaviour live). When False, fall back to legacy close-only logic
    # for direct comparison with prior backtest receipts.
    intrabar_exits: bool = True
    # If both TP and SL are crossed in the same bar, default to stop —
    # we can't know which crossed first, so the conservative call wins.
    intrabar_prefer_stop_when_both_hit: bool = True


@dataclass
class FuturesSessionResult:
    """Outcome of one session backtest."""

    symbol: str
    session_date: str
    trades: list[FuturesClosedTrade]


class FuturesSessionRunner:
    def __init__(
        self,
        runner_cfg: FuturesRunnerConfig,
        slippage_model: FuturesSlippageModel,
        signal_engine: object,
    ) -> None:
        self.cfg = runner_cfg
        self.slippage_model = slippage_model
        self.signal_engine = signal_engine

    def run_session(self, symbol: str, session: FuturesTradingSession) -> FuturesSessionResult:
        bars = session.bars
        if len(bars) < self.cfg.min_signal_bars:
            return FuturesSessionResult(symbol, session.session_date, [])

        contract = self.cfg.symbol_to_contract.get(symbol) or FuturesContract(
            tick_size=0.25, point_value=50.0
        )
        sigma_ann = float(
            self.cfg.underlying_sigma_for_slippage_by_symbol.get(symbol, 0.20)
        )

        runtime_config = {
            "data_quality": {"max_bar_age_minutes": None, "allow_stale_when_closed": True},
            "position_sizing": {"stop_mode": "points"},
            "sentiment": {"enabled": False},
        }

        trades: list[FuturesClosedTrade] = []
        open_position: FuturesOpenPosition | None = None
        entered_today = False
        session_close_et = bars.index[-1].to_pydatetime()

        for i in range(self.cfg.min_signal_bars - 1, len(bars)):
            bar_view = bars.iloc[: i + 1]
            current_time_et = bar_view.index[-1].to_pydatetime()
            current_close = float(bar_view["Close"].iloc[-1])
            current_high = float(bar_view["High"].iloc[-1])
            current_low = float(bar_view["Low"].iloc[-1])
            minutes_to_close = max(
                0.0, (session_close_et - current_time_et).total_seconds() / 60.0
            )

            if open_position is not None:
                trade = self._maybe_exit(
                    open_position, symbol, sigma_ann, contract,
                    current_close, current_high, current_low,
                    current_time_et, minutes_to_close,
                )
                if trade is not None:
                    trades.append(trade)
                    open_position = None

            if open_position is None and not entered_today:
                opened = self._maybe_enter(
                    symbol, bar_view, current_close, current_time_et,
                    sigma_ann, contract, runtime_config,
                )
                if opened is not None:
                    open_position = opened
                    entered_today = True

        if open_position is not None:
            forced = self._force_close(open_position, symbol, sigma_ann, contract, bars)
            if forced is not None:
                trades.append(forced)

        return FuturesSessionResult(symbol, session.session_date, trades)

    def _maybe_enter(
        self,
        symbol: str,
        bar_view: pd.DataFrame,
        current_close: float,
        current_time_et: datetime,
        sigma_ann: float,
        contract: FuturesContract,
        runtime_config: dict,
    ) -> FuturesOpenPosition | None:
        try:
            signal = self.signal_engine.evaluate(symbol, bar_view, runtime_config)
        except Exception:
            return None
        if signal.direction not in ("CALL", "PUT") or signal.reject_reasons:
            return None

        side = "BUY" if signal.direction == "CALL" else "SELL"
        bid, ask = self._bid_ask_around(current_close, contract)

        fill = self.slippage_model.estimate_fill(
            FuturesFillRequest(
                side=side,
                intent="entry",
                bid=bid,
                ask=ask,
                underlying_sigma_ann=sigma_ann,
                quote_age_ms=self.cfg.quote_age_ms,
                decision_to_submit_ms=self.cfg.decision_to_submit_ms,
                submit_to_fill_ms=self.cfg.submit_to_fill_ms,
                now_local_time=current_time_et.time(),
                symbol=symbol,
                qty=self.cfg.contracts_per_trade,
                order_type="market",
            )
        )
        if fill.fill_price is None:
            return None

        return FuturesOpenPosition(
            symbol=symbol,
            side=side,
            contracts=self.cfg.contracts_per_trade,
            entry_time_et=current_time_et,
            entry_price=fill.fill_price,
            point_value=contract.point_value,
            tick_size=contract.tick_size,
            take_profit_points=self.cfg.take_profit_points,
            stop_loss_points=self.cfg.stop_loss_points,
            max_hold_minutes=self.cfg.max_hold_minutes,
        )

    def _maybe_exit(
        self,
        position: FuturesOpenPosition,
        symbol: str,
        sigma_ann: float,
        contract: FuturesContract,
        current_close: float,
        current_high: float,
        current_low: float,
        current_time_et: datetime,
        minutes_to_close: float,
    ) -> FuturesClosedTrade | None:
        if self.cfg.intrabar_exits:
            exit_reason, intrabar_price = evaluate_exit_intrabar(
                position,
                bar_high=current_high,
                bar_low=current_low,
                bar_close=current_close,
                current_time_et=current_time_et,
                minutes_to_session_close=minutes_to_close,
                exit_before_close_minutes=float(self.cfg.exit_before_close_minutes),
                prefer_stop_when_both_hit=self.cfg.intrabar_prefer_stop_when_both_hit,
            )
        else:
            exit_reason = evaluate_exit(
                position,
                current_price=current_close,
                current_time_et=current_time_et,
                minutes_to_session_close=minutes_to_close,
                exit_before_close_minutes=float(self.cfg.exit_before_close_minutes),
            )
            intrabar_price = None

        if exit_reason is None:
            return None

        exit_side = "SELL" if position.side == "BUY" else "BUY"
        intent = "stop" if exit_reason == "stop" else (
            "tp" if exit_reason == "tp" else "time_stop"
        )
        # Center the simulated bid/ask on the actual bracket trigger price
        # when an intrabar TP/SL fired. Centering on current_close would
        # bake in a phantom slippage equal to (close - trigger), which is
        # not what the broker would have filled at.
        price_anchor = intrabar_price if intrabar_price is not None else current_close
        bid, ask = self._bid_ask_around(price_anchor, contract)

        fill = self.slippage_model.estimate_fill(
            FuturesFillRequest(
                side=exit_side,
                intent=intent,
                bid=bid,
                ask=ask,
                underlying_sigma_ann=sigma_ann,
                quote_age_ms=self.cfg.quote_age_ms,
                decision_to_submit_ms=self.cfg.decision_to_submit_ms,
                submit_to_fill_ms=self.cfg.submit_to_fill_ms,
                now_local_time=current_time_et.time(),
                symbol=symbol,
                qty=position.contracts,
                order_type="market",
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
        position: FuturesOpenPosition,
        symbol: str,
        sigma_ann: float,
        contract: FuturesContract,
        bars: pd.DataFrame,
    ) -> FuturesClosedTrade | None:
        last_time = bars.index[-1].to_pydatetime()
        last_close = float(bars["Close"].iloc[-1])
        exit_side = "SELL" if position.side == "BUY" else "BUY"
        bid, ask = self._bid_ask_around(last_close, contract)

        fill = self.slippage_model.estimate_fill(
            FuturesFillRequest(
                side=exit_side,
                intent="time_stop",
                bid=bid,
                ask=ask,
                underlying_sigma_ann=sigma_ann,
                quote_age_ms=self.cfg.quote_age_ms,
                decision_to_submit_ms=self.cfg.decision_to_submit_ms,
                submit_to_fill_ms=self.cfg.submit_to_fill_ms,
                now_local_time=last_time.time(),
                symbol=symbol,
                qty=position.contracts,
                order_type="market",
            )
        )
        exit_price = fill.fill_price if fill.fill_price is not None else last_close
        return close_position(
            position,
            exit_time_et=last_time,
            exit_price=exit_price,
            exit_reason="session_close",
        )

    @staticmethod
    def _bid_ask_around(price: float, contract: FuturesContract) -> tuple[float, float]:
        """Synthetic 1-tick-wide market around the current close."""
        half = contract.tick_size * 0.5
        return price - half, price + half
