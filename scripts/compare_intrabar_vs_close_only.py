"""Compare close-only vs intrabar TP/SL evaluation on the V1 and V2 holdouts.

A code review (2026-05-05) flagged that the futures backtest was evaluating
TP/SL on bar Close only, missing intrabar fires that pulled back. Live
execution will use broker-side OCO which fires on tick prints, so the
backtest's forward expectations should reflect intrabar behaviour too.

This script re-runs the V1 (first) and V2 (second, independent) holdout
windows for ES and NQ under both modes and reports the delta in PF,
Sharpe, win rate, and PnL. The receipts that justified deployment came
from close-only — the question is whether they survive the more
realistic eval.

V2 parameters are LOCKED. We are NOT re-tuning here. We're checking
whether the previously-measured edge holds under intrabar evaluation.

Usage:
    python scripts/compare_intrabar_vs_close_only.py
    python scripts/compare_intrabar_vs_close_only.py --symbols NQ
    python scripts/compare_intrabar_vs_close_only.py --window v2_only
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.metrics import compute_metrics
from src.config import load_config
from src.engines.orb_engine import OrbSignalEngine
from src.futures_backtest.runner import FuturesRunnerConfig, FuturesSessionRunner
from src.futures_backtest.sessions import load_sessions_for_symbol
from src.futures_slippage import FuturesSlippageModel


# Both holdout windows. V1 = first holdout (the one that produced the
# original receipts). V2 = independent fresh window.
HOLDOUT_WINDOWS = {
    "v1_first":  ("2026-02-01", "2026-05-04"),
    "v2_second": ("2023-10-01", "2024-04-30"),
}

# V2 parameter set — frozen from the disciplined tuning sweep.
V2_PARAMS: dict[str, dict[str, float]] = {
    "ES": {"sl_points": 15.0, "tp_points": 30.0},
    "NQ": {"sl_points": 50.0, "tp_points": 100.0},
}

SLIPPAGE_SEED = 42


def _pf_str(metrics) -> str:
    pf = metrics.trade_stats.profit_factor
    if pf is None:
        return "n/a"
    if pf == float("inf"):
        return "inf"
    return f"{pf:.3f}"


def _pf_value(metrics) -> Optional[float]:
    pf = metrics.trade_stats.profit_factor
    if pf is None or pf == float("inf"):
        return None
    return float(pf)


def _run_one(
    symbol: str,
    window: tuple[str, str],
    intrabar: bool,
    config: dict[str, Any],
) -> list:
    """Run all sessions in `window` for `symbol` under the given mode. Returns trades."""
    start, end = window
    sessions = load_sessions_for_symbol(symbol, ROOT / "data" / "historical")
    sessions = [s for s in sessions if start <= s.session_date <= end]
    if not sessions:
        return []

    params = V2_PARAMS[symbol]
    runner_cfg = FuturesRunnerConfig(
        take_profit_points=float(params["tp_points"]),
        stop_loss_points=float(params["sl_points"]),
        intrabar_exits=intrabar,
    )
    slippage = FuturesSlippageModel(rng=random.Random(SLIPPAGE_SEED))
    signal_engine = OrbSignalEngine(config.get("orb", {}))
    runner = FuturesSessionRunner(runner_cfg, slippage, signal_engine=signal_engine)

    trades: list = []
    for s in sessions:
        result = runner.run_session(symbol, s)
        trades.extend(result.trades)
    return trades


def _format_row(label: str, metrics) -> str:
    n = metrics.trade_stats.n_trades
    pnl = metrics.trade_stats.total_pnl
    win = metrics.trade_stats.win_rate
    pf = _pf_str(metrics)
    sharpe = metrics.sharpe_daily_annualized
    sharpe_str = f"{sharpe:.3f}" if sharpe is not None else "n/a"
    return (
        f"  {label:<28s} trades={n:>4}  "
        f"PnL=${pnl:>+10,.0f}  win={win:5.1%}  "
        f"PF={pf:>6s}  Sharpe={sharpe_str:>6s}"
    )


def _delta_str(close_only_v: Optional[float], intrabar_v: Optional[float]) -> str:
    if close_only_v is None or intrabar_v is None:
        return "delta=n/a"
    delta = intrabar_v - close_only_v
    return f"delta={delta:+.3f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["ES", "NQ"],
                        help="Symbols to compare (default: ES NQ)")
    parser.add_argument("--window", choices=["both", "v1_only", "v2_only"], default="both")
    args = parser.parse_args()

    if args.window == "v1_only":
        windows_to_run = {"v1_first": HOLDOUT_WINDOWS["v1_first"]}
    elif args.window == "v2_only":
        windows_to_run = {"v2_second": HOLDOUT_WINDOWS["v2_second"]}
    else:
        windows_to_run = HOLDOUT_WINDOWS

    config = load_config(ROOT / "config.json")

    print("=" * 78)
    print("Intrabar TP/SL vs close-only TP/SL: holdout receipts comparison")
    print("=" * 78)
    print(f"V2 parameters (locked): {json.dumps(V2_PARAMS)}")
    print(f"Slippage seed: {SLIPPAGE_SEED}")
    print()

    for window_name, window in windows_to_run.items():
        print(f"--- Window: {window_name}  ({window[0]} -> {window[1]}) ---")

        for symbol in args.symbols:
            close_trades = _run_one(symbol, window, intrabar=False, config=config)
            intra_trades = _run_one(symbol, window, intrabar=True, config=config)
            if not close_trades and not intra_trades:
                print(f"  {symbol}: no sessions in window; skipping.")
                continue

            close_metrics = compute_metrics(close_trades)
            intra_metrics = compute_metrics(intra_trades)

            print(f"  {symbol}:")
            print(_format_row("close-only:", close_metrics))
            print(_format_row("intrabar:", intra_metrics))

            pf_delta = _delta_str(_pf_value(close_metrics), _pf_value(intra_metrics))
            sharpe_delta = _delta_str(
                close_metrics.sharpe_daily_annualized,
                intra_metrics.sharpe_daily_annualized,
            )
            pnl_delta = (
                intra_metrics.trade_stats.total_pnl - close_metrics.trade_stats.total_pnl
            )
            print(f"    {'deltas:':<28s}{pf_delta:<14s}  {sharpe_delta:<14s}  "
                  f"PnL delta=${pnl_delta:>+,.0f}")

        # Aggregate across symbols within the window
        print()

    # Combined aggregate across all selected windows + symbols
    print("=" * 78)
    print("Combined sample (all selected symbols across all selected windows)")
    print("=" * 78)
    all_close: list = []
    all_intra: list = []
    for window in windows_to_run.values():
        for symbol in args.symbols:
            all_close.extend(_run_one(symbol, window, intrabar=False, config=config))
            all_intra.extend(_run_one(symbol, window, intrabar=True, config=config))

    if not all_close and not all_intra:
        print("No trades produced.")
        return 1

    cm = compute_metrics(all_close)
    im = compute_metrics(all_intra)
    print(_format_row("close-only:", cm))
    print(_format_row("intrabar:", im))
    pf_delta = _delta_str(_pf_value(cm), _pf_value(im))
    sharpe_delta = _delta_str(cm.sharpe_daily_annualized, im.sharpe_daily_annualized)
    pnl_delta = im.trade_stats.total_pnl - cm.trade_stats.total_pnl
    print(f"  {'deltas:':<28s}{pf_delta:<14s}  {sharpe_delta:<14s}  "
          f"PnL delta=${pnl_delta:>+,.0f}")

    # Gate check on intrabar metrics — that's what live deployment will see.
    print()
    print("Gates on intrabar receipts (live-aligned):")
    pf = _pf_value(im)
    sharpe = im.sharpe_daily_annualized
    print(f"  PF > 1.05    : {_pf_str(im)}  "
          f"{'PASS' if pf is not None and pf > 1.05 else 'FAIL'}")
    if sharpe is None:
        print("  Sharpe > 0.5 : n/a    FAIL")
    else:
        print(f"  Sharpe > 0.5 : {sharpe:.3f}  "
              f"{'PASS' if sharpe > 0.5 else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
