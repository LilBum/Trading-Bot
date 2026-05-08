"""Futures backtest CLI.

Examples:
    python -m src.futures_backtest --symbol ES
    python -m src.futures_backtest --symbol NQ --start-date 2025-01-01 --end-date 2025-12-31
    python -m src.futures_backtest --symbol ES --tp-points 20 --sl-points 8
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from src.backtest.metrics import compute_metrics, format_metrics_summary
from src.config import load_config
from src.engines.orb_engine import OrbSignalEngine
from src.futures_backtest.runner import FuturesRunnerConfig, FuturesSessionRunner
from src.futures_backtest.sessions import load_sessions_for_symbol
from src.futures_slippage import FuturesSlippageModel


def main() -> int:
    parser = argparse.ArgumentParser(description="Futures ORB backtest (CME e-minis)")
    parser.add_argument("--symbol", required=True, help="Symbol to backtest (e.g., ES, NQ)")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-date", type=str, default=None,
                        help="Filter sessions to dates >= this (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, default=None,
                        help="Filter sessions to dates <= this (YYYY-MM-DD)")
    parser.add_argument("--tp-points", type=float, default=100.0,
                        help="Take-profit threshold in price points (V2-NQ default)")
    parser.add_argument("--sl-points", type=float, default=50.0,
                        help="Stop-loss threshold in price points (V2-NQ default)")
    parser.add_argument("--max-hold-minutes", type=int, default=120)
    parser.add_argument("--exit-before-close-minutes", type=int, default=5)
    parser.add_argument("--contracts", type=int, default=1)
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

    if args.start_date:
        sessions = [s for s in sessions if s.session_date >= args.start_date]
    if args.end_date:
        sessions = [s for s in sessions if s.session_date <= args.end_date]
    if not sessions:
        print(f"No sessions for {args.symbol} after filtering.", file=sys.stderr)
        return 1

    runner_cfg = FuturesRunnerConfig(
        take_profit_points=args.tp_points,
        stop_loss_points=args.sl_points,
        max_hold_minutes=args.max_hold_minutes,
        exit_before_close_minutes=args.exit_before_close_minutes,
        contracts_per_trade=args.contracts,
    )
    slippage = FuturesSlippageModel(rng=random.Random(args.seed))
    signal_engine = OrbSignalEngine(config.get("orb", {}))
    runner = FuturesSessionRunner(runner_cfg, slippage, signal_engine=signal_engine)

    print(f"Symbol:   {args.symbol}  Signal: orb")
    print(f"Sessions: {len(sessions)}  ({sessions[0].session_date} -> {sessions[-1].session_date})")
    print(f"Exits:    TP=+{args.tp_points}pts  SL=-{args.sl_points}pts  "
          f"max_hold={args.max_hold_minutes}min  contracts={args.contracts}")

    all_trades: list = []
    for session in sessions:
        result = runner.run_session(args.symbol, session)
        all_trades.extend(result.trades)

    print("\n--- Single-block metrics ---")
    print(format_metrics_summary(compute_metrics(all_trades)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
