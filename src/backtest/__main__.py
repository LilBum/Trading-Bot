"""Backtest CLI.

Examples:
    python -m src.backtest --symbol SPY
    python -m src.backtest --symbol QQQ --walk-forward
    python -m src.backtest --symbol GLD --signal orb
    python -m src.backtest --symbol SPY --signal orb --walk-forward --train-days 120
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from src.backtest.metrics import (
    compute_metrics,
    format_metrics_summary,
)
from src.backtest.runner import RunnerConfig, SessionRunner
from src.backtest.sessions import load_sessions_for_symbol
from src.backtest.walk_forward import (
    WalkForwardConfig,
    aggregate_oos_trades,
    run_walk_forward,
)
from src.config import load_config
from src.engines.orb_engine import OrbSignalEngine
from src.slippage import SlippageModel


def main() -> int:
    parser = argparse.ArgumentParser(description="1DTE options strategy backtest")
    parser.add_argument("--symbol", required=True, help="Symbol to backtest, e.g. SPY")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for slippage model")
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Run walk-forward windows; otherwise run all sessions in one block",
    )
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument(
        "--signal",
        choices=("vwap_pullback", "orb"),
        default="vwap_pullback",
        help="Which signal engine to use",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 1

    try:
        sessions = load_sessions_for_symbol(args.symbol, args.data_dir)
    except FileNotFoundError as exc:
        print(f"Bars file missing: {exc}", file=sys.stderr)
        return 1

    if not sessions:
        print(f"No sessions found for {args.symbol} in {args.data_dir}", file=sys.stderr)
        return 1

    # ORB only needs the opening range (~15 bars) before it can fire,
    # so we lower the gate that exists for VWAP-pullback's EMA warmup.
    min_signal_bars = 16 if args.signal == "orb" else 30
    runner_cfg = RunnerConfig(
        strategy_cfg=config["strategy"],
        exits_cfg=config["exits"],
        min_signal_bars=min_signal_bars,
    )
    slippage = SlippageModel(rng=random.Random(args.seed))
    if args.signal == "orb":
        signal_engine = OrbSignalEngine(config.get("orb", {}))
    else:
        signal_engine = None  # SessionRunner falls back to VwapPullbackSignalEngine
    runner = SessionRunner(runner_cfg, slippage, signal_engine=signal_engine)

    print(f"Symbol:   {args.symbol}  Signal: {args.signal}")
    print(f"Sessions: {len(sessions)}  ({sessions[0].session_date} -> {sessions[-1].session_date})")

    if args.walk_forward:
        wf_cfg = WalkForwardConfig(
            train_window_days=args.train_days,
            test_window_days=args.test_days,
            step_days=args.step_days,
        )
        windows = run_walk_forward(runner, args.symbol, sessions, wf_cfg)
        if not windows:
            print("No walk-forward windows produced (insufficient data range).", file=sys.stderr)
            return 1
        for w in windows:
            print(
                f"  window {w.window_index:>2}: "
                f"train [{w.train_start} -> {w.train_end})  "
                f"test [{w.test_start} -> {w.test_end})  "
                f"trades={len(w.test_trades)}"
            )
        oos_trades = aggregate_oos_trades(windows)
        print("\n--- OOS aggregated metrics ---")
        print(format_metrics_summary(compute_metrics(oos_trades)))
    else:
        all_trades: list = []
        for s in sessions:
            result = runner.run_session(args.symbol, s)
            all_trades.extend(result.trades)
        print("\n--- Single-block metrics ---")
        print(format_metrics_summary(compute_metrics(all_trades)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
