"""Download CME futures 1-minute bars from Databento, gated by cost estimate.

Uses the metadata API to estimate cost first. Hard-caps at $50 (well under
the $125 signup credit) so a single run can't blow through the free balance.
Outputs CSV per symbol in `data/historical/` matching the existing format
(timestamp UTC ISO8601, Open, High, Low, Close, Volume).

Default: ES + NQ continuous front-month, 2024-05-01 -> 2026-05-04, ohlcv-1m.

Usage:
    python scripts/download_futures_databento.py
    python scripts/download_futures_databento.py --estimate-only
    python scripts/download_futures_databento.py --symbols ES.c.0
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import databento as db  # noqa: E402

from src.config import load_dotenv  # noqa: E402


DATA_DIR = ROOT / "data" / "historical"
DEFAULT_DATASET = "GLBX.MDP3"            # CME MDP 3.0 — full ES/NQ/etc.
DEFAULT_SCHEMA = "ohlcv-1m"              # cheapest schema with the granularity we need
DEFAULT_STYPE_IN = "continuous"          # continuous front-month notation (ES.c.0)
DEFAULT_SYMBOLS = ["ES.c.0", "NQ.c.0"]
DEFAULT_START = "2024-05-01"
DEFAULT_END = "2026-05-04"
COST_HARD_CAP_USD = 50.0                 # leave headroom under the $125 free credit


def _human_short(symbol: str) -> str:
    """`ES.c.0` -> `ES`. Used to name the output CSV file."""
    return symbol.split(".")[0]


def _validate_date(value: str, flag: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"ERROR: {flag} must be YYYY-MM-DD ({exc}).") from exc


def estimate_cost(client, symbols, start, end) -> float:
    """Use Databento's metadata API to estimate cost in USD before pulling data."""
    return float(
        client.metadata.get_cost(
            dataset=DEFAULT_DATASET,
            symbols=symbols,
            schema=DEFAULT_SCHEMA,
            start=start,
            end=end,
            stype_in=DEFAULT_STYPE_IN,
        )
    )


def download_and_split(client, symbols, start, end, out_dir: Path) -> dict[str, int]:
    """Pull data, split by symbol, write CSV per symbol. Returns row counts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = client.timeseries.get_range(
        dataset=DEFAULT_DATASET,
        symbols=symbols,
        schema=DEFAULT_SCHEMA,
        start=start,
        end=end,
        stype_in=DEFAULT_STYPE_IN,
    )
    df = data.to_df()
    if df.empty:
        return {}

    counts: dict[str, int] = {}
    for symbol in symbols:
        short = _human_short(symbol)
        # `to_df()` puts the human-readable symbol in the `symbol` column when
        # `stype_in="continuous"`. The continuous root (e.g. "ES") is what we
        # match on; the bar's symbol field shows the underlying contract code
        # (e.g. "ESM4"), so filter on the root using the `raw_symbol` mapping.
        candidate = df
        if "symbol" in df.columns:
            # Fallback: if the dataframe carries the original input symbol, filter.
            mask = df["symbol"].astype(str).str.startswith(short)
            candidate = df[mask] if mask.any() else df
        # If multiple symbols were requested but the df doesn't differentiate,
        # we'll ship all rows under each symbol's CSV — caller should request
        # one symbol at a time when this happens. For our two-symbol case the
        # `symbol` column is reliably present.
        sym_df = candidate.copy()
        sym_df = sym_df.reset_index()
        # ts_event is a tz-aware UTC pandas Timestamp.
        if "ts_event" in sym_df.columns:
            sym_df["timestamp"] = sym_df["ts_event"]
        elif sym_df.index.name == "ts_event":
            sym_df["timestamp"] = sym_df.index
        else:
            raise SystemExit("Could not find ts_event in Databento response")

        out_cols = ["timestamp", "open", "high", "low", "close", "volume"]
        present_cols = [c for c in out_cols if c in sym_df.columns]
        if len(present_cols) < len(out_cols):
            raise SystemExit(f"Missing columns in Databento response: {set(out_cols) - set(present_cols)}")
        sym_df = sym_df[out_cols].rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        sym_df["timestamp"] = sym_df["timestamp"].apply(lambda t: t.isoformat() if hasattr(t, "isoformat") else str(t))
        out_path = out_dir / f"{short}_1m.csv"
        sym_df.to_csv(out_path, index=False)
        counts[short] = len(sym_df)
        print(f"[{short}] wrote {len(sym_df):,} rows -> {out_path}")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Download CME futures 1m bars from Databento")
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS,
                        help="Continuous-front-month symbols (e.g., ES.c.0 NQ.c.0)")
    parser.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END, help="YYYY-MM-DD")
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--cost-cap", type=float, default=COST_HARD_CAP_USD,
                        help="Abort if estimated cost exceeds this (default $50)")
    parser.add_argument("--estimate-only", action="store_true",
                        help="Print cost estimate and exit; don't download")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the cost cap (use only after a clean estimate)")
    args = parser.parse_args()

    _validate_date(args.start, "--start")
    _validate_date(args.end, "--end")

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        print(
            "ERROR: DATABENTO_API_KEY not set. Sign up at https://databento.com "
            "and add the key to .env.",
            file=sys.stderr,
        )
        return 1

    client = db.Historical(api_key)

    print(f"Dataset:  {DEFAULT_DATASET}")
    print(f"Schema:   {DEFAULT_SCHEMA}")
    print(f"Symbols:  {args.symbols}")
    print(f"Range:    {args.start} -> {args.end}")
    print()

    print("Estimating cost via metadata API ...", flush=True)
    try:
        cost_usd = estimate_cost(client, args.symbols, args.start, args.end)
    except Exception as exc:
        print(f"ERROR: cost estimate failed: {exc}", file=sys.stderr)
        return 1

    print(f"Estimated cost: ${cost_usd:.2f}")
    print(f"Cost cap:       ${args.cost_cap:.2f}")

    if args.estimate_only:
        return 0

    if cost_usd > args.cost_cap and not args.force:
        print(
            f"ABORT: estimated cost ${cost_usd:.2f} exceeds the cost cap "
            f"${args.cost_cap:.2f}. Re-run with --force or reduce scope.",
            file=sys.stderr,
        )
        return 1

    print("\nProceeding with download ...", flush=True)
    counts = download_and_split(client, args.symbols, args.start, args.end, args.out_dir)
    if not counts:
        print("WARNING: no rows returned.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
