"""Smoke-test IBKR paper connection. Read-only — no orders placed.

Connects to TWS / Gateway on the configured host:port, verifies the connected
account is a paper account, prints a small summary, and disconnects.

Usage:
    python scripts/verify_ibkr_paper_connection.py
    python scripts/verify_ibkr_paper_connection.py --port 7497
    python scripts/verify_ibkr_paper_connection.py --client-id 7

If TWS shows a "Allow incoming connection?" popup the first time, click
"Allow always" or "Yes" to whitelist this client.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.futures_execution.ibkr import IBKRConnectionConfig
from src.futures_execution.ibkr_connect import (
    NonPaperAccountError,
    connect_with_safety_check,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify IBKR paper connection (read-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496,
                        help="TWS paper: 7497, TWS live: 7496, Gateway paper: 4002, Gateway live: 4001")
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument("--allow-live", action="store_true",
                        help="DON'T USE during shakedown. Disables paper-only safety check.")
    args = parser.parse_args()

    cfg = IBKRConnectionConfig(host=args.host, port=args.port, client_id=args.client_id)
    print(f"Connecting to {args.host}:{args.port} (clientId={args.client_id})...", flush=True)

    try:
        ib, accounts = connect_with_safety_check(cfg, paper_only=not args.allow_live)
    except NonPaperAccountError as exc:
        print(f"\nSAFETY ABORT: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nERROR: connection failed: {exc}", file=sys.stderr)
        print("\nCommon causes:")
        print("  - TWS / Gateway not running")
        print("  - Wrong port (paper TWS=7497, live TWS=7496, paper GW=4002, live GW=4001)")
        print("  - 'Enable ActiveX and Socket Clients' is unchecked in TWS API settings")
        print("  - First connection: TWS may be showing a popup asking to allow the client")
        return 1

    try:
        print(f"\nConnected. Managed accounts: {accounts}")
        primary = accounts[0]
        print(f"Primary account:   {primary}")
        print(f"Paper account?     {primary.startswith('DU')}")

        try:
            summary = ib.accountSummary(primary)
            interesting = {
                row.tag: row.value for row in summary
                if row.tag in (
                    "NetLiquidation", "AvailableFunds", "BuyingPower",
                    "MaintMarginReq", "FullInitMarginReq",
                )
            }
            if interesting:
                print("\nAccount summary (selected fields):")
                for tag, value in interesting.items():
                    print(f"  {tag:25s}: {value}")
        except Exception as exc:
            print(f"\n(Could not fetch account summary: {exc})")

        positions = ib.positions(primary)
        print(f"\nOpen positions: {len(positions)}")
        for p in positions:
            print(f"  {p.contract.symbol}  qty={p.position}  avg_cost={p.avgCost}")

        print("\nConnection test PASSED. Adapter is ready for paper-mode integration.")
    finally:
        ib.disconnect()
        print("Disconnected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
