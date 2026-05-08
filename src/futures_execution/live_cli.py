"""Live futures runner CLI.

End-to-end entrypoint for an unattended NQ→MNQ paper (or live) session:

  1. Connects to IB Gateway / TWS with the paper-only safety check
  2. Builds the IBKR-backed bars provider, quote provider, and execution
     adapter using front-month real Future contracts (not ContFuture)
  3. Recovers state from the broker if a position from a prior process
     run is still open (reconcile-on-start)
  4. Runs the DrivingLoop from session_start_et to session_end_et,
     ticking the LivePaperRunner every `tick_seconds` and journalling
     each iteration to JSONL
  5. Disconnects cleanly at session end

Examples:
    # Paper TWS, NQ signal → MNQ execution, 30s ticks:
    python -m src.futures_execution.live_cli

    # Dry run: connect, recover state, print plan, exit without trading:
    python -m src.futures_execution.live_cli --dry-run

    # Custom symbols and account:
    python -m src.futures_execution.live_cli \\
        --signal-symbol NQ --execution-symbol MNQ \\
        --account DUQ822130 --port 7496
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.engines.orb_engine import OrbSignalEngine  # noqa: E402
from src.futures_execution.driving_loop import (  # noqa: E402
    DrivingLoop,
    DrivingLoopConfig,
)
from src.futures_execution.ibkr import (  # noqa: E402
    IBKRConnectionConfig,
    IBKRFuturesExecutionAdapter,
    IBKRFuturesQuoteProvider,
)
from src.futures_execution.ibkr_bars import IBKRBarsProvider  # noqa: E402
from src.futures_execution.ibkr_connect import (  # noqa: E402
    NonPaperAccountError,
    connect_with_safety_check,
)
from src.futures_execution.journal import IterationJournal  # noqa: E402
from src.futures_execution.live_runner import (  # noqa: E402
    LivePaperRunner,
    LivePaperRunnerConfig,
)


EASTERN = ZoneInfo("America/New_York")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the live futures ORB strategy against IBKR (paper or live).",
    )
    p.add_argument("--signal-symbol", default="NQ",
                   help="Symbol for bars + signal generation (default: NQ -where the edge is measured)")
    p.add_argument("--execution-symbol", default="MNQ",
                   help="Symbol for order routing (default: MNQ -1/10th NQ for shakedown)")
    p.add_argument("--account", default=None,
                   help="IBKR account ID (e.g. DUQ822130). If omitted, ib_insync uses the default.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7496,
                   help="7496=Paper TWS, 7497=Live TWS, 4002=Paper GW, 4001=Live GW")
    p.add_argument("--client-id", type=int, default=7)
    p.add_argument("--paper-only", action=argparse.BooleanOptionalAction, default=True,
                   help="Reject any non-DU* account at connect time (default: True)")
    p.add_argument("--tick-seconds", type=int, default=30)
    p.add_argument("--session-start-et", type=_parse_hhmm, default="08:00",
                   help="HH:MM ET -when the bot wakes (signal anchor is later, see config.orb)")
    p.add_argument("--session-end-et", type=_parse_hhmm, default="16:00",
                   help="HH:MM ET -when the bot exits cleanly")
    p.add_argument("--contracts", type=int, default=1)
    p.add_argument("--tp-points", type=float, default=100.0)
    p.add_argument("--sl-points", type=float, default=50.0)
    p.add_argument("--exit-before-close-minutes", type=int, default=5)
    p.add_argument("--min-signal-bars", type=int, default=16)
    p.add_argument("--journal-path", type=Path, default=Path("live_journal.jsonl"))
    p.add_argument("--config", type=Path, default=Path("config.json"))
    p.add_argument("--dry-run", action="store_true",
                   help="Connect, recover state, print plan, exit without trading")
    return p


def _print_plan(args, ibkr_account: Optional[str]) -> None:
    print("---- Live runner plan ----")
    print(f"  Signal symbol:    {args.signal_symbol}")
    print(f"  Execution symbol: {args.execution_symbol}")
    print(f"  IBKR host:port:   {args.host}:{args.port} (clientId={args.client_id})")
    print(f"  Account:          {ibkr_account or '(default)'}")
    print(f"  Paper-only:       {args.paper_only}")
    print(f"  Session window:   {args.session_start_et}-{args.session_end_et} ET")
    print(f"  Tick cadence:     {args.tick_seconds}s")
    print(f"  Per-trade size:   {args.contracts} contract(s) of {args.execution_symbol}")
    print(f"  TP / SL:          +{args.tp_points}pt / -{args.sl_points}pt")
    print(f"  Pre-close exit:   {args.exit_before_close_minutes} min before close")
    print(f"  Journal:          {args.journal_path}")
    print(f"  Dry run:          {args.dry_run}")
    print("--------------------------")


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Load project config (.env loader pulls DATABENTO_API_KEY, etc -not all
    # used here, but the call also validates plaintext-secret hygiene).
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Failed to load config from {args.config}: {exc}", file=sys.stderr)
        return 1

    # Resolve account from CLI, then config, then None (let ib_insync default).
    ibkr_account = args.account or (config.get("execution", {})
                                          .get("ibkr", {})
                                          .get("account"))

    # Connect with paper-only safety net.
    cfg = IBKRConnectionConfig(
        host=args.host, port=args.port, client_id=args.client_id,
        account=ibkr_account,
    )
    print(f"Connecting to {cfg.host}:{cfg.port} (clientId={cfg.client_id}) ...")
    try:
        ib, accounts = connect_with_safety_check(cfg, paper_only=args.paper_only)
    except NonPaperAccountError as exc:
        print(f"\nSAFETY ABORT: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nERROR: connection failed: {exc}", file=sys.stderr)
        return 1
    print(f"Connected. Accounts: {accounts}")
    if ibkr_account is None and accounts:
        # Pin to the first account so adapter calls don't ambiguously route.
        ibkr_account = accounts[0]

    return_code = 0
    try:
        # Build adapter / bars / signal / runner / journal / loop stack.
        adapter = IBKRFuturesExecutionAdapter(
            ib_client=ib,
            account=ibkr_account,
        )
        bars_provider = IBKRBarsProvider(
            ib_client=ib,
            session_start_et=args.session_start_et,
        )
        signal_engine = OrbSignalEngine(config.get("orb", {}))

        runner_cfg = LivePaperRunnerConfig(
            take_profit_points=args.tp_points,
            stop_loss_points=args.sl_points,
            contracts_per_trade=args.contracts,
            session_start_et=args.session_start_et,
            session_end_et=args.session_end_et,
            exit_before_close_minutes=args.exit_before_close_minutes,
            min_signal_bars=args.min_signal_bars,
        )
        runner = LivePaperRunner(
            signal_engine=signal_engine,
            execution_adapter=adapter,
            bars_provider=bars_provider,
            config=runner_cfg,
            signal_symbol=args.signal_symbol,
            execution_symbol=args.execution_symbol,
        )

        _print_plan(args, ibkr_account)

        # Reconcile-on-start: if a position from a prior crash is still
        # open at the broker, recover state before the loop wakes up.
        recovered = runner.recover_state_from_broker()
        if recovered is not None:
            print(f"\nRECOVERED open position at broker: "
                  f"{recovered.side} {recovered.qty} {args.execution_symbol} "
                  f"@ {recovered.entry_price}")
            print("Runner state populated; loop will manage existing position.")
        else:
            print("\nNo pre-existing position at broker.")

        if args.dry_run:
            print("\nDry-run mode -exiting before loop start.")
            return 0

        journal = IterationJournal(args.journal_path)
        loop_cfg = DrivingLoopConfig(
            session_start_et=args.session_start_et,
            session_end_et=args.session_end_et,
            tick_seconds=args.tick_seconds,
        )
        loop = DrivingLoop(runner=runner, journal=journal, config=loop_cfg)

        print(f"\nEntering driving loop. Session window: "
              f"{args.session_start_et}-{args.session_end_et} ET. "
              f"Tick: {args.tick_seconds}s.")
        iterations = loop.run()
        print(f"\nSession complete. Iterations: {iterations}. "
              f"Journal: {args.journal_path}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return_code = 130
    except Exception as exc:
        print(f"\nUnhandled exception: {exc}", file=sys.stderr)
        return_code = 1
    finally:
        try:
            ib.disconnect()
            print("Disconnected from IBKR.")
        except Exception:
            pass

    return return_code


if __name__ == "__main__":
    sys.exit(main())
