"""Extend the existing ES/NQ history with an earlier Databento chunk.

Downloads only the missing earlier window, then merges into the existing
data/historical/{SYMBOL}_1m.csv files (preserves later data, dedupes by
timestamp, sorts). Capped at $10 estimated cost as a safety net.

Usage:
    python scripts/extend_futures_history.py
    python scripts/extend_futures_history.py --start 2023-01-01 --end 2023-09-30
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import databento as db  # noqa: E402
import pandas as pd  # noqa: E402

from src.config import load_dotenv  # noqa: E402


DATA_DIR = ROOT / "data" / "historical"
DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
STYPE_IN = "continuous"
DEFAULT_SYMBOLS = ["ES.c.0", "NQ.c.0"]
DEFAULT_NEW_START = "2023-10-01"
DEFAULT_NEW_END = "2024-04-30"
COST_HARD_CAP_USD = 10.0


def _short(symbol: str) -> str:
    return symbol.split(".")[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Extend ES/NQ history with an earlier chunk")
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    parser.add_argument("--start", default=DEFAULT_NEW_START, help="YYYY-MM-DD inclusive")
    parser.add_argument("--end", default=DEFAULT_NEW_END, help="YYYY-MM-DD inclusive")
    parser.add_argument("--cost-cap", type=float, default=COST_HARD_CAP_USD)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        print("ERROR: DATABENTO_API_KEY not set", file=sys.stderr)
        return 1

    client = db.Historical(api_key)

    print(f"Range:    {args.start} -> {args.end}")
    print(f"Symbols:  {args.symbols}")
    print()

    cost = float(
        client.metadata.get_cost(
            dataset=DATASET, symbols=args.symbols, schema=SCHEMA,
            start=args.start, end=args.end, stype_in=STYPE_IN,
        )
    )
    print(f"Estimated cost: ${cost:.2f}")
    print(f"Cost cap:       ${args.cost_cap:.2f}")
    if cost > args.cost_cap:
        print("ABORT: estimated cost exceeds cap.", file=sys.stderr)
        return 1

    print("\nDownloading...", flush=True)
    try:
        data = client.timeseries.get_range(
            dataset=DATASET, symbols=args.symbols, schema=SCHEMA,
            start=args.start, end=args.end, stype_in=STYPE_IN,
        )
    except Exception as exc:
        print(f"ERROR: download failed: {exc}", file=sys.stderr)
        return 1

    new_df = data.to_df().reset_index()
    if "ts_event" in new_df.columns:
        new_df["timestamp"] = new_df["ts_event"]
    elif new_df.index.name == "ts_event":
        new_df["timestamp"] = new_df.index
    else:
        print("ERROR: ts_event missing from response", file=sys.stderr)
        return 1

    print(f"Downloaded {len(new_df):,} new rows total. Splitting and merging...")

    for symbol in args.symbols:
        short = _short(symbol)
        existing_path = DATA_DIR / f"{short}_1m.csv"

        if "symbol" in new_df.columns:
            mask = new_df["symbol"].astype(str).str.startswith(short)
            sym_new = new_df[mask].copy()
        else:
            sym_new = new_df.copy()

        if sym_new.empty:
            print(f"[{short}] no new rows for symbol; skipping merge.")
            continue

        sym_new = sym_new[
            ["timestamp", "open", "high", "low", "close", "volume"]
        ].rename(
            columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            }
        )
        sym_new["timestamp"] = sym_new["timestamp"].apply(
            lambda t: t.isoformat() if hasattr(t, "isoformat") else str(t)
        )

        if existing_path.exists():
            existing = pd.read_csv(existing_path)
        else:
            existing = pd.DataFrame(columns=sym_new.columns)

        combined = pd.concat([sym_new, existing], ignore_index=True)
        combined["__ts"] = pd.to_datetime(combined["timestamp"])
        combined = combined.drop_duplicates(subset="__ts").sort_values("__ts")
        combined = combined.drop(columns=["__ts"])

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        combined.to_csv(existing_path, index=False)
        added = len(sym_new)
        total = len(combined)
        print(f"[{short}] merged: +{added:,} new rows  ->  {total:,} total in {existing_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
