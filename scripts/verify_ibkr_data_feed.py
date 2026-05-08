"""Verify IBKR is feeding LIVE futures data (not delayed).

Connects, sets market data type to live, subscribes to MNQ (or whatever
--symbol), and reports whether the feed is live, frozen, or delayed. Also
pulls the last 30 minutes of 1m historical bars to confirm the bar pipeline
the live runner will use actually works.

If the marketDataType comes back as 3 or 4 (delayed), CME real-time data
subscription is needed (~$11/mo non-pro at IBKR) before the ORB strategy
can run live — the entire signal depends on tracking the opening range as
it forms in real time.

Usage:
    python scripts/verify_ibkr_data_feed.py
    python scripts/verify_ibkr_data_feed.py --symbol NQ --port 7497
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


def _has_real_value(x) -> bool:
    """True iff x is a non-zero, non-None, non-NaN number."""
    if x is None:
        return False
    if isinstance(x, float) and math.isnan(x):
        return False
    if x == 0:
        return False
    return True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.futures_execution.ibkr import (
    IBKRConnectionConfig,
    resolve_front_month_future,
)
from src.futures_execution.ibkr_connect import (
    NonPaperAccountError,
    connect_with_safety_check,
)


_MARKET_DATA_LABEL = {
    1: "LIVE",
    2: "FROZEN (market closed; last live quote)",
    3: "DELAYED (10-15 min lag)",
    4: "DELAYED-FROZEN",
}


def _fmt(value, default: str = "n/a"):
    if value is None or value == 0 or (isinstance(value, float) and value != value):  # NaN check
        return default
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=2)
    parser.add_argument("--symbol", default="MNQ", help="ES, NQ, MES, MNQ")
    parser.add_argument("--ticker-wait-seconds", type=float, default=5.0)
    args = parser.parse_args()

    cfg = IBKRConnectionConfig(host=args.host, port=args.port, client_id=args.client_id)
    print(f"Connecting to {args.host}:{args.port} (clientId={args.client_id})...", flush=True)

    try:
        ib, accounts = connect_with_safety_check(cfg, paper_only=True)
    except NonPaperAccountError as exc:
        print(f"\nSAFETY ABORT: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nERROR: connection failed: {exc}", file=sys.stderr)
        return 1

    print(f"Connected. Account(s): {accounts}")
    return_code = 0

    try:
        from ib_insync import ContFuture

        # Request live market data; falls back automatically if subscription absent.
        ib.reqMarketDataType(1)
        print(f"Requested market data type: 1 (live).\n")

        # Resolve the active front-month real Future for live quotes.
        # ContFuture is historical-data-only at IBKR — we must use Future
        # for reqMktData / reqTickers / placeOrder. We still use ContFuture
        # below for the historical bar pipeline check, which is its
        # supported use.
        contract_cache: dict = {}
        try:
            contract = resolve_front_month_future(
                ib, args.symbol, contract_cache
            )
        except Exception as exc:
            print(f"ERROR: could not resolve front-month for {args.symbol}: {exc}",
                  file=sys.stderr)
            return 1
        print(f"Resolved front-month: {getattr(contract, 'localSymbol', '') or args.symbol} "
              f"(expiration {contract.lastTradeDateOrContractMonth})")

        # Subscribe to streaming quotes.
        ticker = ib.reqMktData(contract, "", False, False)
        print(f"Streaming subscription requested. Waiting {args.ticker_wait_seconds}s for ticker...")
        ib.sleep(args.ticker_wait_seconds)

        # Quote snapshot
        print(f"\n--- {args.symbol} ticker snapshot ---")
        print(f"  bid:           {_fmt(ticker.bid)}")
        print(f"  ask:           {_fmt(ticker.ask)}")
        print(f"  last:          {_fmt(ticker.last)}")
        print(f"  bid size:      {_fmt(ticker.bidSize)}")
        print(f"  ask size:      {_fmt(ticker.askSize)}")
        print(f"  volume:        {_fmt(ticker.volume)}")
        print(f"  ticker.time:   {ticker.time}")

        mdt = ticker.marketDataType
        mdt_label = _MARKET_DATA_LABEL.get(mdt, f"UNKNOWN ({mdt})")
        print(f"  marketDataType: {mdt} -> {mdt_label}")

        # Verdict on data feed.
        # `marketDataType` alone can lie — IBKR sometimes reports type=1 (live)
        # while delivering no data because the underlying entitlement is missing.
        # ib_insync returns NaN floats for missing fields, and `bool(nan)` is
        # truthy in Python — so check for real numeric values explicitly.
        has_quote_data = _has_real_value(ticker.bid) and _has_real_value(ticker.ask)

        print(f"\n--- Feed verdict ---")
        feed_ok_for_live = False
        if mdt == 1 and has_quote_data:
            print(">>> LIVE FEED CONFIRMED. Bid/ask populated, marketDataType=1.")
            print("    ORB strategy is data-supported.")
            feed_ok_for_live = True
        elif mdt == 1 and not has_quote_data:
            print(">>> Feed reports LIVE but delivered no quote data.")
            print("    This is the IBKR signature of a missing CME subscription:")
            print("    `marketDataType` returns 1 by default, but bid/ask stay empty")
            print("    because the underlying entitlement is gated.")
            print("    Subscribe to CME Real-Time (NP, L1) at IBKR (~$11/mo, often")
            print("    waived at $20+/mo commissions) before live trading.")
            return_code = 2
        elif mdt == 2:
            print(">>> FROZEN: market currently closed; feed should resume when CME reopens.")
            print("    (CME futures trade ~23 hours; this is unusual mid-week unless we're in")
            print("     the daily 5pm-6pm ET maintenance break.)")
        elif mdt in (3, 4):
            print(">>> DELAYED FEED. THIS IS A BLOCKER for the ORB strategy.")
            print("    Subscribe to CME Real-Time Data at IBKR:")
            print("    https://www.interactivebrokers.com/en/pricing/market-data-pricing.php")
            print("    Cost: ~$11/mo non-pro, often waived at $20+/mo commissions.")
            return_code = 2
        else:
            print(">>> UNCLEAR feed status. Manual investigation needed.")
            return_code = 2

        # Quote age sanity check
        if isinstance(ticker.time, datetime):
            t = ticker.time if ticker.time.tzinfo else ticker.time.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - t).total_seconds()
            print(f"\nQuote age: {age:.1f}s")
            if mdt == 1 and age > 30:
                print("    (live but stale — could be a quiet market or feed lag)")

        # Historical bars sanity check.
        # Production's IBKRBarsProvider uses ContFuture for reqHistoricalData
        # (its supported use — auto-rolls across expiries). Mirror that here
        # so the verify script tests exactly the path the live runner takes.
        print(f"\n--- 1-minute bar pipeline test (ContFuture path) ---")
        bars_contract = contract  # default fallback
        try:
            cf = ContFuture(args.symbol, exchange="CME", currency="USD")
            qualified_cf = ib.qualifyContracts(cf)
            if qualified_cf:
                bars_contract = qualified_cf[0]
                print(f"Bars contract: ContFuture {args.symbol} "
                      f"(secType={getattr(bars_contract, 'secType', 'CONTFUT')})")
            else:
                print("WARNING: ContFuture qualify returned empty; "
                      "falling back to front-month Future for bars test.")
        except Exception as exc:
            print(f"WARNING: ContFuture resolution failed ({exc}); "
                  f"falling back to front-month Future for bars test.")

        bars = ib.reqHistoricalData(
            bars_contract,
            endDateTime="",
            durationStr="1800 S",  # 30 minutes (=1800 seconds)
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        if not bars:
            print(">>> No historical bars returned.")
            return_code = 2
        else:
            print(f"Pulled {len(bars)} bars. Latest 3:")
            for b in bars[-3:]:
                print(f"  {b.date}  O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume}")
            latest = bars[-1].date
            if isinstance(latest, datetime):
                t = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
                print(f"\nLatest bar age: {age_min:.1f} minutes")
                if age_min > 5:
                    print(">>> WARNING: latest bar is >5min old. May indicate delayed feed.")

        ib.cancelMktData(contract)

    finally:
        ib.disconnect()
        print("\nDisconnected.")

    if return_code == 0 and feed_ok_for_live:
        print("\n>>> READY: live data feed and bar pipeline both working.")
    elif return_code != 0:
        print("\n>>> NOT READY: subscription or pipeline issue above. Fix before live trading.")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
