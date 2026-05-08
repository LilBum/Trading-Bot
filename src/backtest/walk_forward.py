"""Walk-forward driver for the backtest runner.

Splits sessions into rolling (train, test) windows and reports per-window
out-of-sample trades. With no hyperparameter tuning yet the "train" slice
is informational only — but the structure is here so when tuning lands we
fit on the train slice and evaluate on the test slice without touching
the next window's data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.backtest.positions import ClosedTrade
from src.backtest.runner import SessionRunner
from src.backtest.sessions import TradingSession


@dataclass(frozen=True)
class WalkForwardConfig:
    train_window_days: int = 180  # 6 months
    test_window_days: int = 60    # 2 months
    step_days: int = 30           # 1 month forward step


@dataclass(frozen=True)
class WalkForwardWindow:
    window_index: int
    train_start: str
    train_end: str   # exclusive
    test_start: str  # = train_end
    test_end: str    # exclusive
    test_trades: list[ClosedTrade]


def run_walk_forward(
    runner: SessionRunner,
    symbol: str,
    sessions: list[TradingSession],
    cfg: WalkForwardConfig,
) -> list[WalkForwardWindow]:
    if not sessions:
        return []

    ordered = sorted(sessions, key=lambda s: s.session_date)
    first_date = date.fromisoformat(ordered[0].session_date)
    last_date = date.fromisoformat(ordered[-1].session_date)

    by_date: dict[str, TradingSession] = {s.session_date: s for s in ordered}
    sorted_dates = [s.session_date for s in ordered]

    windows: list[WalkForwardWindow] = []
    train_start_date = first_date
    window_index = 0

    while True:
        train_end_date = train_start_date + timedelta(days=cfg.train_window_days)
        test_end_date = train_end_date + timedelta(days=cfg.test_window_days)

        # Stop when the test window runs past the last session entirely.
        if train_end_date > last_date:
            break

        test_dates = [
            d for d in sorted_dates
            if train_end_date.isoformat() <= d < test_end_date.isoformat()
        ]
        if not test_dates:
            train_start_date = train_start_date + timedelta(days=cfg.step_days)
            window_index += 1
            continue

        test_trades: list[ClosedTrade] = []
        for ds in test_dates:
            result = runner.run_session(symbol, by_date[ds])
            test_trades.extend(result.trades)

        windows.append(
            WalkForwardWindow(
                window_index=window_index,
                train_start=train_start_date.isoformat(),
                train_end=train_end_date.isoformat(),
                test_start=train_end_date.isoformat(),
                test_end=test_end_date.isoformat(),
                test_trades=test_trades,
            )
        )

        train_start_date = train_start_date + timedelta(days=cfg.step_days)
        window_index += 1

    return windows


def aggregate_oos_trades(windows: list[WalkForwardWindow]) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for w in windows:
        out.extend(w.test_trades)
    return out
