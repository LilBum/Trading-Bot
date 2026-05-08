"""Futures ORB tuning sweep with strict holdout discipline.

Train/tune on 2024-05-01 -> 2026-01-31. Hold out 2026-02-01 -> 2026-05-04.
Picks winner by aggregate PF on the tuning window. Evaluates winner on holdout
ONCE. Gates at PF > 1.05 AND Sharpe > 0.5 before declaring edge.

Variants are parameterized per-symbol because NQ has ~3x ES intraday volatility,
so the same point-based stop/target is structurally mismatched between them.

Usage:
    python scripts/run_futures_tuning_sweep.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.metrics import compute_metrics, format_metrics_summary
from src.config import load_config
from src.engines.orb_engine import OrbSignalEngine
from src.futures_backtest.runner import FuturesRunnerConfig, FuturesSessionRunner
from src.futures_backtest.sessions import (
    FuturesTradingSession,
    load_sessions_for_symbol,
)
from src.futures_slippage import FuturesSlippageModel


SYMBOLS = ["ES", "NQ"]
TUNING_END = "2026-01-31"        # inclusive
HOLDOUT_START = "2026-02-01"     # inclusive
SLIPPAGE_SEED = 42

# Each variant maps symbol -> {sl_points, tp_points}.
# NQ values scaled ~3x because NQ's intraday range is ~3x ES.
VARIANTS: dict[str, dict[str, dict[str, float]]] = {
    "V0_baseline_unscaled": {
        "ES": {"sl_points": 12.0, "tp_points": 40.0},
        "NQ": {"sl_points": 12.0, "tp_points": 40.0},
    },
    "V1_per_symbol_scaled": {
        "ES": {"sl_points": 12.0, "tp_points": 40.0},
        "NQ": {"sl_points": 40.0, "tp_points": 120.0},
    },
    "V2_tighter_ratio_2to1": {
        "ES": {"sl_points": 15.0, "tp_points": 30.0},
        "NQ": {"sl_points": 50.0, "tp_points": 100.0},
    },
    "V3_wider_ratio_5to1": {
        "ES": {"sl_points": 10.0, "tp_points": 50.0},
        "NQ": {"sl_points": 30.0, "tp_points": 150.0},
    },
}


def filter_sessions(
    sessions: list[FuturesTradingSession],
    start: str | None,
    end: str | None,
) -> list[FuturesTradingSession]:
    out = sessions
    if start:
        out = [s for s in out if s.session_date >= start]
    if end:
        out = [s for s in out if s.session_date <= end]
    return out


def run_variant(
    config: dict,
    symbol_overrides: dict[str, dict[str, float]],
    sessions_by_symbol: dict[str, list[FuturesTradingSession]],
    symbols: list[str],
) -> tuple[list, dict[str, list]]:
    """Run one variant across all symbols. Returns (all_trades, per_symbol_trades)."""
    all_trades: list = []
    per_symbol: dict[str, list] = {}
    for symbol in symbols:
        sym_overrides = symbol_overrides.get(symbol, {})
        sl = float(sym_overrides.get("sl_points", 12.0))
        tp = float(sym_overrides.get("tp_points", 40.0))
        runner_cfg = FuturesRunnerConfig(
            take_profit_points=tp,
            stop_loss_points=sl,
        )
        slippage = FuturesSlippageModel(rng=random.Random(SLIPPAGE_SEED))
        signal_engine = OrbSignalEngine(config.get("orb", {}))
        runner = FuturesSessionRunner(runner_cfg, slippage, signal_engine=signal_engine)

        symbol_trades: list = []
        for session in sessions_by_symbol[symbol]:
            result = runner.run_session(symbol, session)
            symbol_trades.extend(result.trades)
        per_symbol[symbol] = symbol_trades
        all_trades.extend(symbol_trades)
    return all_trades, per_symbol


def _pf_or_zero(metrics) -> float:
    pf = metrics.trade_stats.profit_factor
    if pf is None:
        return 0.0
    if pf == float("inf"):
        return float("inf")
    return float(pf)


def _print_per_symbol(per_symbol: dict[str, list], symbols: list[str]) -> None:
    for symbol in symbols:
        sm = compute_metrics(per_symbol[symbol])
        pf_val = sm.trade_stats.profit_factor
        if pf_val is None:
            pf_str = "n/a"
        elif pf_val == float("inf"):
            pf_str = "inf"
        else:
            pf_str = f"{pf_val:.2f}"
        print(
            f"  {symbol}: trades={sm.trade_stats.n_trades:>4}  "
            f"win={sm.trade_stats.win_rate:.1%}  "
            f"PnL=${sm.trade_stats.total_pnl:>+10,.0f}  PF={pf_str}"
        )


def main() -> int:
    config = load_config(ROOT / "config.json")
    sessions_by_symbol = {
        sym: load_sessions_for_symbol(sym, ROOT / "data" / "historical")
        for sym in SYMBOLS
    }

    tuning = {sym: filter_sessions(sessions_by_symbol[sym], None, TUNING_END) for sym in SYMBOLS}
    holdout = {sym: filter_sessions(sessions_by_symbol[sym], HOLDOUT_START, None) for sym in SYMBOLS}

    first_session = sessions_by_symbol["ES"][0].session_date
    last_session = sessions_by_symbol["ES"][-1].session_date

    print(f"Tuning window:  {first_session} -> {TUNING_END}  ({len(tuning['ES'])} ES sessions)")
    print(f"Holdout window: {HOLDOUT_START} -> {last_session}  ({len(holdout['ES'])} ES sessions)")
    print(f"Symbols:        {SYMBOLS}")
    print(f"Slippage seed:  {SLIPPAGE_SEED}")
    print()

    print("=" * 72)
    print("TUNING WINDOW")
    print("=" * 72)
    variant_results: dict[str, dict] = {}
    for name, overrides in VARIANTS.items():
        print(f"\n--- {name} ---")
        print(f"Per-symbol exits: {json.dumps(overrides)}")
        all_trades, per_symbol = run_variant(config, overrides, tuning, SYMBOLS)
        m = compute_metrics(all_trades)
        variant_results[name] = {"metrics": m, "all_trades": all_trades, "per_symbol": per_symbol}
        print(format_metrics_summary(m))
        print("Per symbol:")
        _print_per_symbol(per_symbol, SYMBOLS)

    winner_name = max(VARIANTS.keys(), key=lambda n: _pf_or_zero(variant_results[n]["metrics"]))
    winner_pf = _pf_or_zero(variant_results[winner_name]["metrics"])
    print()
    print(f">>> Winner on tuning window: {winner_name} (aggregate PF={winner_pf:.3f})")

    print()
    print("=" * 72)
    print(f"HOLDOUT EVALUATION (touched ONCE): {winner_name}")
    print("=" * 72)
    holdout_trades, holdout_per_symbol = run_variant(
        config, VARIANTS[winner_name], holdout, SYMBOLS
    )
    holdout_metrics = compute_metrics(holdout_trades)
    print(format_metrics_summary(holdout_metrics))
    print("Per symbol:")
    _print_per_symbol(holdout_per_symbol, SYMBOLS)

    holdout_pf = _pf_or_zero(holdout_metrics)
    holdout_sharpe = holdout_metrics.sharpe_daily_annualized

    print()
    print("Gate check:")
    print(f"  PF > 1.05         : {holdout_pf:.3f}  {'PASS' if holdout_pf > 1.05 else 'FAIL'}")
    if holdout_sharpe is None:
        print("  Sharpe > 0.5      : n/a   FAIL")
        sharpe_pass = False
    else:
        sharpe_pass = holdout_sharpe > 0.5
        print(f"  Sharpe > 0.5      : {holdout_sharpe:.3f}  {'PASS' if sharpe_pass else 'FAIL'}")

    if (holdout_pf > 1.05) and sharpe_pass:
        print("\n>>> EDGE CONFIRMED on strict holdout. Proceed to Phase 4 (deployment).")
    else:
        print(
            "\n>>> GATE FAILED. Strategy does not have demonstrated edge on the holdout. "
            "Don't curve-fit by re-tuning."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
