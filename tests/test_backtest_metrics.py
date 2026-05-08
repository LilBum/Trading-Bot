import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.backtest.metrics import (
    compute_calmar,
    compute_drawdown,
    compute_metrics,
    compute_sharpe,
    compute_trade_stats,
    daily_pnl_series,
    format_metrics_summary,
)
from src.backtest.positions import ClosedTrade


EASTERN = ZoneInfo("America/New_York")


def _trade(pnl: float, exit_day: int = 1, entry_price: float = 1.0, exit_price: float | None = None) -> ClosedTrade:
    """Build a ClosedTrade with pnl pinned and other fields plausible."""
    if exit_price is None:
        exit_price = entry_price + pnl / 100.0  # 1 contract default
    return ClosedTrade(
        symbol="SPY",
        direction="CALL",
        strike=500.0,
        contracts=1,
        entry_time_et=datetime(2026, 5, exit_day, 10, 0, tzinfo=EASTERN),
        exit_time_et=datetime(2026, 5, exit_day, 11, 0, tzinfo=EASTERN),
        entry_price=entry_price,
        exit_price=exit_price,
        realized_pnl=pnl,
        exit_reason="tp" if pnl > 0 else "stop",
        holding_minutes=60.0,
    )


# ----- compute_trade_stats ----------------------------------------------


def test_trade_stats_empty():
    s = compute_trade_stats([])
    assert s.n_trades == 0
    assert s.n_wins == 0
    assert s.n_losses == 0
    assert s.win_rate == 0.0
    assert s.total_pnl == 0.0
    assert s.profit_factor is None


def test_trade_stats_all_wins():
    s = compute_trade_stats([_trade(100), _trade(50, exit_day=2)])
    assert s.n_trades == 2
    assert s.n_wins == 2
    assert s.n_losses == 0
    assert s.win_rate == 1.0
    assert s.total_pnl == 150.0
    assert s.profit_factor == math.inf


def test_trade_stats_mixed():
    s = compute_trade_stats(
        [_trade(100), _trade(-50, exit_day=2), _trade(75, exit_day=3), _trade(-25, exit_day=4)]
    )
    assert s.n_trades == 4
    assert s.n_wins == 2
    assert s.n_losses == 2
    assert s.win_rate == 0.5
    assert s.total_pnl == 100.0
    assert s.gross_win == 175.0
    assert s.gross_loss == 75.0
    assert s.profit_factor == pytest.approx(175.0 / 75.0)
    assert s.avg_win == pytest.approx(87.5)
    assert s.avg_loss == pytest.approx(-37.5)


def test_trade_stats_all_losses_returns_zero_profit_factor_or_none():
    s = compute_trade_stats([_trade(-50), _trade(-30, exit_day=2)])
    assert s.profit_factor == 0.0  # gross_win=0, gross_loss>0 → ratio=0


# ----- daily_pnl_series -------------------------------------------------


def test_daily_pnl_series_empty():
    assert daily_pnl_series([]).empty


def test_daily_pnl_series_groups_by_date():
    trades = [
        _trade(100, exit_day=1),
        _trade(-30, exit_day=1),
        _trade(50, exit_day=2),
    ]
    series = daily_pnl_series(trades)
    assert len(series) == 2
    assert series.iloc[0] == pytest.approx(70.0)
    assert series.iloc[1] == pytest.approx(50.0)
    assert series.index.is_monotonic_increasing


# ----- compute_drawdown -------------------------------------------------


def test_drawdown_empty_series():
    dd = compute_drawdown(pd.Series(dtype=float))
    assert dd.max_drawdown == 0.0
    assert dd.peak_equity == 0.0


def test_drawdown_no_losses_means_zero_dd():
    series = pd.Series([100.0, 50.0, 20.0])
    dd = compute_drawdown(series)
    assert dd.max_drawdown == 0.0


def test_drawdown_finds_largest_peak_to_trough():
    # cumulative equity: 100, 150, 100, 200, 150, 50, 100
    # running peak:     100, 150, 150, 200, 200, 200, 200
    # drawdown:           0,   0,  50,   0,  50, 150, 100
    series = pd.Series([100.0, 50.0, -50.0, 100.0, -50.0, -100.0, 50.0])
    dd = compute_drawdown(series)
    assert dd.max_drawdown == pytest.approx(150.0)
    assert dd.peak_equity == pytest.approx(200.0)
    assert dd.max_drawdown_pct == pytest.approx(0.75)


# ----- compute_sharpe ---------------------------------------------------


def test_sharpe_returns_none_for_too_few_observations():
    assert compute_sharpe(pd.Series([100.0])) is None
    assert compute_sharpe(pd.Series(dtype=float)) is None


def test_sharpe_returns_none_when_stdev_zero():
    assert compute_sharpe(pd.Series([100.0, 100.0, 100.0])) is None


def test_sharpe_positive_when_returns_positive():
    series = pd.Series([100.0, 50.0, 75.0, 60.0, 80.0])
    sharpe = compute_sharpe(series)
    assert sharpe is not None
    assert sharpe > 0


def test_sharpe_negative_when_returns_negative():
    series = pd.Series([-100.0, -50.0, -75.0, -60.0, -80.0])
    sharpe = compute_sharpe(series)
    assert sharpe is not None
    assert sharpe < 0


def test_sharpe_annualized_to_252():
    # If daily mean = 1, daily std = 1, then SR_daily = 1, annualized = sqrt(252).
    series = pd.Series([0.0, 2.0] * 50)  # mean=1, ddof=1 std≈1.005
    sharpe = compute_sharpe(series)
    assert sharpe is not None
    assert sharpe == pytest.approx(math.sqrt(252) * 1 / series.std(ddof=1), rel=1e-6)


# ----- compute_calmar ---------------------------------------------------


def test_calmar_returns_none_when_no_drawdown():
    assert compute_calmar(pd.Series([10.0, 20.0, 30.0])) is None


def test_calmar_returns_none_for_empty_series():
    assert compute_calmar(pd.Series(dtype=float)) is None


def test_calmar_positive_for_winning_strategy_with_some_drawdown():
    # PnL: +50, -30, +100 → equity 50, 20, 120; peak 50, 50, 120; dd 0, 30, 0; max_dd=30
    # mean = 40. Annualized = 40 * 252 = 10080. Calmar = 10080 / 30 = 336.
    series = pd.Series([50.0, -30.0, 100.0])
    calmar = compute_calmar(series)
    assert calmar == pytest.approx(40.0 * 252 / 30.0, rel=1e-6)


# ----- compute_metrics --------------------------------------------------


def test_compute_metrics_end_to_end():
    trades = [
        _trade(100, exit_day=1),
        _trade(-50, exit_day=2),
        _trade(75, exit_day=3),
        _trade(-25, exit_day=4),
    ]
    m = compute_metrics(trades)
    assert m.trade_stats.n_trades == 4
    assert m.trade_stats.total_pnl == 100.0
    assert m.n_trading_days == 4
    # Sharpe and Calmar should be computable.
    assert m.sharpe_daily_annualized is not None
    assert m.drawdown.max_drawdown >= 0


def test_compute_metrics_empty_input():
    m = compute_metrics([])
    assert m.trade_stats.n_trades == 0
    assert m.drawdown.max_drawdown == 0.0
    assert m.sharpe_daily_annualized is None
    assert m.calmar is None
    assert m.n_trading_days == 0


def test_format_metrics_summary_includes_key_fields():
    trades = [_trade(100, exit_day=1), _trade(-50, exit_day=2)]
    summary = format_metrics_summary(compute_metrics(trades))
    assert "Trades: 2" in summary
    assert "Win rate: 50.0%" in summary
    assert "Total PnL" in summary
    assert "Sharpe" in summary
    assert "Exits:" in summary


def test_compute_metrics_includes_exit_reason_breakdown():
    # _trade() defaults exit_reason to "tp" for wins, "stop" for losses.
    trades = [
        _trade(100, exit_day=1),
        _trade(-50, exit_day=2),
        _trade(-30, exit_day=3),
    ]
    m = compute_metrics(trades)
    assert m.exit_reason_counts == {"tp": 1, "stop": 2}
    assert m.exit_reason_pnl["tp"] == pytest.approx(100.0)
    assert m.exit_reason_pnl["stop"] == pytest.approx(-80.0)


def test_format_metrics_summary_renders_dd_pct_safely_when_no_positive_peak():
    # Strategy that never went positive: peak_equity ≤ 0.
    trades = [_trade(-50, exit_day=1), _trade(-30, exit_day=2)]
    summary = format_metrics_summary(compute_metrics(trades))
    assert "n/a (no positive peak)" in summary
