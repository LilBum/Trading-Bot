"""Second-holdout test: run V2 (winner of the first tuning sweep) on a fresh
prior window that was not present during V2 selection.

V2 parameters were locked when the first tuning sweep ran (2024-05-01 ->
2026-01-31 tuning, 2026-02-01 -> 2026-05-04 holdout). The first holdout
yielded PF 1.055 PASS / Sharpe 0.435 FAIL — borderline.

This script runs that same V2 (NOT re-tuned) on 2023-10-01 -> 2024-04-30,
which Databento data we just acquired. The point is independent confirmation
or rejection of the marginal-edge hypothesis without burning more
multiple-testing budget.

Same gates: PF > 1.05 AND Sharpe > 0.5.

Usage:
    python scripts/run_futures_second_holdout.py
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
from src.futures_backtest.sessions import load_sessions_for_symbol
from src.futures_slippage import FuturesSlippageModel


SYMBOLS = ["ES", "NQ"]
SECOND_HOLDOUT_START = "2023-10-01"   # inclusive
SECOND_HOLDOUT_END = "2024-04-30"     # inclusive
SLIPPAGE_SEED = 42

# V2 = winner from project_phase3_futures_orb.md. Frozen — DO NOT TUNE on this window.
V2_PARAMS: dict[str, dict[str, float]] = {
    "ES": {"sl_points": 15.0, "tp_points": 30.0},
    "NQ": {"sl_points": 50.0, "tp_points": 100.0},
}


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

    print(f"Second holdout window: {SECOND_HOLDOUT_START} -> {SECOND_HOLDOUT_END}")
    print(f"Variant: V2 (frozen from first tuning sweep — NOT re-tuned)")
    print(f"Per-symbol exits: {json.dumps(V2_PARAMS)}")
    print(f"Slippage seed: {SLIPPAGE_SEED}")
    print()

    all_trades: list = []
    per_symbol: dict[str, list] = {}
    for symbol in SYMBOLS:
        sessions = load_sessions_for_symbol(symbol, ROOT / "data" / "historical")
        sessions = [
            s for s in sessions
            if SECOND_HOLDOUT_START <= s.session_date <= SECOND_HOLDOUT_END
        ]
        if not sessions:
            print(f"WARNING: no sessions for {symbol} in window; skipping.", file=sys.stderr)
            per_symbol[symbol] = []
            continue

        params = V2_PARAMS[symbol]
        runner_cfg = FuturesRunnerConfig(
            take_profit_points=float(params["tp_points"]),
            stop_loss_points=float(params["sl_points"]),
        )
        slippage = FuturesSlippageModel(rng=random.Random(SLIPPAGE_SEED))
        signal_engine = OrbSignalEngine(config.get("orb", {}))
        runner = FuturesSessionRunner(runner_cfg, slippage, signal_engine=signal_engine)

        symbol_trades: list = []
        for session in sessions:
            result = runner.run_session(symbol, session)
            symbol_trades.extend(result.trades)
        per_symbol[symbol] = symbol_trades
        all_trades.extend(symbol_trades)
        print(f"  {symbol}: {len(sessions)} sessions, {len(symbol_trades)} trades")

    if not all_trades:
        print("ABORT: no trades produced. Did the data download cover this window?", file=sys.stderr)
        return 1

    metrics = compute_metrics(all_trades)
    print()
    print("=" * 72)
    print("SECOND HOLDOUT RESULT (independent confirmation test)")
    print("=" * 72)
    print(format_metrics_summary(metrics))
    print("\nPer symbol:")
    _print_per_symbol(per_symbol, SYMBOLS)

    pf = _pf_or_zero(metrics)
    sharpe = metrics.sharpe_daily_annualized

    print()
    print("Gate check:")
    print(f"  PF > 1.05      : {pf:.3f}  {'PASS' if pf > 1.05 else 'FAIL'}")
    if sharpe is None:
        print("  Sharpe > 0.5   : n/a   FAIL")
        sharpe_pass = False
    else:
        sharpe_pass = sharpe > 0.5
        print(f"  Sharpe > 0.5   : {sharpe:.3f}  {'PASS' if sharpe_pass else 'FAIL'}")

    print()
    print("Comparison to first holdout (2026-02-01 -> 2026-05-01):")
    print("  First holdout  : PF=1.055 PASS, Sharpe=0.435 FAIL  (borderline)")
    print(f"  Second holdout : PF={pf:.3f} {'PASS' if pf > 1.05 else 'FAIL'}, "
          f"Sharpe={sharpe:.3f} {'PASS' if sharpe_pass else 'FAIL'}")

    if pf > 1.05 and sharpe_pass:
        print("\n>>> BOTH gates PASS on independent holdout. Edge confirmed enough to proceed.")
    elif pf > 1.05 and not sharpe_pass:
        print("\n>>> PF holds, Sharpe still misses. Two PF passes + one near-Sharpe is suggestive.")
        print("    Still doesn't strictly clear the AND-gate, but the signal-noise ratio is real.")
    elif sharpe_pass and not (pf > 1.05):
        print("\n>>> Sharpe holds but PF regresses. Suggests first-holdout PF was sampling noise.")
    else:
        print("\n>>> Both gates FAIL on second holdout. Marginal-edge hypothesis rejected.")
        print("    Time to look hard at the system or pivot to a different approach.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
