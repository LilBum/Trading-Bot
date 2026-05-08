"""ORB tuning sweep with strict holdout discipline.

Train/tune on 2024-05-06 -> 2026-01-31. Hold out 2026-02-01 -> 2026-05-04
(touched ONCE at the end). Pick winner by aggregate profit factor on the
tuning window. Evaluate winner on holdout. Gate at PF > 1.05 AND Sharpe > 0.5
before declaring edge.

Usage:
    python scripts/run_tuning_sweep.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.metrics import compute_metrics, format_metrics_summary
from src.backtest.runner import RunnerConfig, SessionRunner
from src.backtest.sessions import TradingSession, load_sessions_for_symbol
from src.config import load_config
from src.engines.orb_engine import OrbSignalEngine
from src.slippage import SlippageModel


SYMBOLS = ["SPY", "QQQ", "GLD", "SLV", "NVDA", "AMZN"]
TUNING_END = "2026-01-31"        # inclusive
HOLDOUT_START = "2026-02-01"     # inclusive
SLIPPAGE_SEED = 42

VARIANTS: dict[str, dict] = {
    "V0_baseline": {
        "orb": {"require_retest_or_hold": False, "hold_bars": 1, "breakout_buffer_points": 0.0},
        "exits": {"take_profit_pct": 0.30, "stop_loss_pct": 0.25},
    },
    "V1_quality_filter": {
        "orb": {"require_retest_or_hold": True, "hold_bars": 2, "breakout_buffer_points": 0.0},
        "exits": {"take_profit_pct": 0.30, "stop_loss_pct": 0.25},
    },
    "V2_asymmetric_exits": {
        "orb": {"require_retest_or_hold": False, "hold_bars": 1, "breakout_buffer_points": 0.0},
        "exits": {"take_profit_pct": 0.40, "stop_loss_pct": 0.20},
    },
    "V3_combined": {
        "orb": {"require_retest_or_hold": True, "hold_bars": 2, "breakout_buffer_points": 0.0},
        "exits": {"take_profit_pct": 0.40, "stop_loss_pct": 0.20},
    },
}


def filter_sessions(
    sessions: list[TradingSession],
    start: str | None,
    end: str | None,
) -> list[TradingSession]:
    out = sessions
    if start:
        out = [s for s in out if s.session_date >= start]
    if end:
        out = [s for s in out if s.session_date <= end]
    return out


def run_variant(
    config: dict,
    overrides: dict,
    sessions_by_symbol: dict[str, list[TradingSession]],
    symbols: list[str],
) -> tuple[list, dict[str, list]]:
    """Run one variant across all symbols. Returns (all_trades, per_symbol_trades)."""
    base_orb = {**config.get("orb", {}), **overrides.get("orb", {})}
    base_exits = {**config.get("exits", {}), **overrides.get("exits", {})}

    all_trades: list = []
    per_symbol: dict[str, list] = {}
    for symbol in symbols:
        runner_cfg = RunnerConfig(
            strategy_cfg=config["strategy"],
            exits_cfg=base_exits,
            min_signal_bars=16,
        )
        # Fresh slippage stream per symbol so the rng draws are stable across variants.
        slippage = SlippageModel(rng=random.Random(SLIPPAGE_SEED))
        signal_engine = OrbSignalEngine(base_orb)
        runner = SessionRunner(runner_cfg, slippage, signal_engine=signal_engine)

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
            f"  {symbol}: trades={sm.trade_stats.n_trades:>3}  "
            f"win={sm.trade_stats.win_rate:.1%}  "
            f"PnL=${sm.trade_stats.total_pnl:>+9,.0f}  PF={pf_str}"
        )


def main() -> int:
    config = load_config(ROOT / "config.json")
    sessions_by_symbol = {
        sym: load_sessions_for_symbol(sym, ROOT / "data" / "historical") for sym in SYMBOLS
    }

    tuning = {sym: filter_sessions(sessions_by_symbol[sym], None, TUNING_END) for sym in SYMBOLS}
    holdout = {sym: filter_sessions(sessions_by_symbol[sym], HOLDOUT_START, None) for sym in SYMBOLS}

    first_session = sessions_by_symbol["SPY"][0].session_date
    last_session = sessions_by_symbol["SPY"][-1].session_date
    tuning_count = len(tuning["SPY"])
    holdout_count = len(holdout["SPY"])

    print(f"Tuning window:  {first_session} -> {TUNING_END}  ({tuning_count} sessions)")
    print(f"Holdout window: {HOLDOUT_START} -> {last_session}  ({holdout_count} sessions)")
    print(f"Symbols:        {SYMBOLS}")
    print(f"Slippage seed:  {SLIPPAGE_SEED}")
    print()

    print("=" * 72)
    print("TUNING WINDOW")
    print("=" * 72)
    variant_results: dict[str, dict] = {}
    for name, overrides in VARIANTS.items():
        print(f"\n--- {name} ---")
        print(f"Overrides: {json.dumps(overrides)}")
        all_trades, per_symbol = run_variant(config, overrides, tuning, SYMBOLS)
        m = compute_metrics(all_trades)
        variant_results[name] = {"metrics": m, "all_trades": all_trades, "per_symbol": per_symbol}
        print(format_metrics_summary(m))
        print("Per symbol:")
        _print_per_symbol(per_symbol, SYMBOLS)

    winner_name = max(VARIANTS.keys(), key=lambda n: _pf_or_zero(variant_results[n]["metrics"]))
    winner_pf = _pf_or_zero(variant_results[winner_name]["metrics"])
    print()
    print(">>> Winner on tuning window: " f"{winner_name} (aggregate PF={winner_pf:.3f})")

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
        print("\n>>> EDGE CONFIRMED on strict holdout. Proceed to Phase 3 execution hardening.")
    else:
        print(
            "\n>>> GATE FAILED. Strategy does not have demonstrated edge on the holdout. "
            "Don't curve-fit by re-tuning — escalate decision (different signal class / kill 1DTE)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
