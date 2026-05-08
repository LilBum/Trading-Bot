"""Backtest performance metrics.

Trade-level: count, win rate, profit factor, average win/loss.
Equity-curve-level: max drawdown, Sharpe ratio (daily-PnL annualized), Calmar.

PSR/DSR/PBO are deferred until walk-forward + CPCV are wired; they require
multi-trial calibration that isn't useful with a single backtest run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from src.backtest.positions import ClosedTrade


@dataclass(frozen=True)
class TradeStats:
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    total_pnl: float
    gross_win: float
    gross_loss: float
    profit_factor: float | None
    avg_win: float
    avg_loss: float


@dataclass(frozen=True)
class DrawdownStats:
    max_drawdown: float
    max_drawdown_pct: float
    peak_equity: float


@dataclass(frozen=True)
class PerformanceMetrics:
    trade_stats: TradeStats
    drawdown: DrawdownStats
    sharpe_daily_annualized: float | None
    calmar: float | None
    n_trading_days: int
    exit_reason_counts: dict[str, int]
    exit_reason_pnl: dict[str, float]


_TRADING_DAYS_PER_YEAR = 252


def compute_trade_stats(trades: Iterable[ClosedTrade]) -> TradeStats:
    trade_list = list(trades)
    n = len(trade_list)
    wins = [t for t in trade_list if t.realized_pnl > 0]
    losses = [t for t in trade_list if t.realized_pnl < 0]
    gross_win = sum(t.realized_pnl for t in wins)
    gross_loss = -sum(t.realized_pnl for t in losses)
    total_pnl = sum(t.realized_pnl for t in trade_list)
    profit_factor: float | None
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = math.inf
    else:
        profit_factor = None
    return TradeStats(
        n_trades=n,
        n_wins=len(wins),
        n_losses=len(losses),
        win_rate=(len(wins) / n) if n else 0.0,
        total_pnl=total_pnl,
        gross_win=gross_win,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        avg_win=(gross_win / len(wins)) if wins else 0.0,
        avg_loss=(-gross_loss / len(losses)) if losses else 0.0,
    )


def daily_pnl_series(trades: Iterable[ClosedTrade]) -> pd.Series:
    """Sum trade PnL by ET exit date. Returns a tz-naive Series indexed by date."""
    rows: dict[str, float] = {}
    for t in trades:
        date_key = t.exit_time_et.date().isoformat()
        rows[date_key] = rows.get(date_key, 0.0) + t.realized_pnl
    if not rows:
        return pd.Series(dtype=float)
    series = pd.Series(rows, dtype=float)
    series.index = pd.to_datetime(series.index)
    return series.sort_index()


def compute_drawdown(daily_pnl: pd.Series) -> DrawdownStats:
    if daily_pnl.empty:
        return DrawdownStats(max_drawdown=0.0, max_drawdown_pct=0.0, peak_equity=0.0)
    equity = daily_pnl.cumsum()
    running_peak = equity.cummax()
    drawdown = running_peak - equity
    max_dd = float(drawdown.max())
    peak = float(running_peak.max())
    pct = (max_dd / peak) if peak > 0 else 0.0
    return DrawdownStats(max_drawdown=max_dd, max_drawdown_pct=pct, peak_equity=peak)


def compute_sharpe(daily_pnl: pd.Series, rf_per_year: float = 0.0) -> float | None:
    """Daily-PnL Sharpe annualized to 252 trading days. None if undefined."""
    if daily_pnl.empty or len(daily_pnl) < 2:
        return None
    rf_per_day = rf_per_year / _TRADING_DAYS_PER_YEAR
    excess = daily_pnl - rf_per_day
    std = excess.std(ddof=1)
    if std is None or std == 0 or math.isnan(std):
        return None
    return float(excess.mean() / std * math.sqrt(_TRADING_DAYS_PER_YEAR))


def compute_calmar(daily_pnl: pd.Series) -> float | None:
    """Annualized PnL / max drawdown. None if max DD is zero."""
    if daily_pnl.empty:
        return None
    annualized = float(daily_pnl.mean() * _TRADING_DAYS_PER_YEAR)
    dd = compute_drawdown(daily_pnl)
    if dd.max_drawdown <= 0:
        return None
    return annualized / dd.max_drawdown


def compute_metrics(trades: Iterable[ClosedTrade]) -> PerformanceMetrics:
    trade_list = list(trades)
    stats = compute_trade_stats(trade_list)
    pnl = daily_pnl_series(trade_list)
    counts: dict[str, int] = {}
    by_reason_pnl: dict[str, float] = {}
    for t in trade_list:
        counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
        by_reason_pnl[t.exit_reason] = by_reason_pnl.get(t.exit_reason, 0.0) + t.realized_pnl
    return PerformanceMetrics(
        trade_stats=stats,
        drawdown=compute_drawdown(pnl),
        sharpe_daily_annualized=compute_sharpe(pnl),
        calmar=compute_calmar(pnl),
        n_trading_days=len(pnl),
        exit_reason_counts=counts,
        exit_reason_pnl=by_reason_pnl,
    )


def format_metrics_summary(metrics: PerformanceMetrics) -> str:
    """One-screen human-readable performance summary."""
    s = metrics.trade_stats
    d = metrics.drawdown
    pf = "inf" if s.profit_factor == math.inf else (
        f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a"
    )
    sharpe = f"{metrics.sharpe_daily_annualized:.2f}" if metrics.sharpe_daily_annualized is not None else "n/a"
    calmar = f"{metrics.calmar:.2f}" if metrics.calmar is not None else "n/a"
    dd_pct = (
        f"{d.max_drawdown_pct:.1%}" if d.peak_equity > 0 else "n/a (no positive peak)"
    )
    exits_line = "  ".join(
        f"{reason}: {count} (${metrics.exit_reason_pnl.get(reason, 0.0):,.0f})"
        for reason, count in sorted(metrics.exit_reason_counts.items())
    ) or "(none)"
    return (
        f"Trades: {s.n_trades}  Wins: {s.n_wins}  Losses: {s.n_losses}  "
        f"Win rate: {s.win_rate:.1%}\n"
        f"Total PnL: ${s.total_pnl:,.2f}  Profit factor: {pf}  "
        f"Avg win: ${s.avg_win:,.2f}  Avg loss: ${s.avg_loss:,.2f}\n"
        f"Max DD: ${d.max_drawdown:,.2f} ({dd_pct})  "
        f"Peak equity: ${d.peak_equity:,.2f}\n"
        f"Sharpe (daily, ann.): {sharpe}  Calmar: {calmar}  "
        f"Trading days: {metrics.n_trading_days}\n"
        f"Exits: {exits_line}"
    )
