"""Test Databento Historical API freshness for same-day data.

If Historical can serve data within ~2 minutes of real-time, we use it
as a poor-man's live feed during the IBKR-funding window. If it's
hours/days lagged, we pivot to a proper Live API integration.

Tests two endpoints:
1. metadata.get_cost — confirms same-day range is even valid
2. timeseries.get_range — pulls the data, reports age of latest bar
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import databento as db  # noqa: E402

from src.config import load_dotenv  # noqa: E402


DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
SYMBOL = "MNQ.c.0"


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        print("ERROR: DATABENTO_API_KEY not set", file=sys.stderr)
        return 1

    client = db.Historical(api_key)

    # Request from 2 hours ago to "now"
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(hours=2)

    start_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    end_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Testing Databento Historical freshness")
    print(f"  Dataset: {DATASET}")
    print(f"  Schema:  {SCHEMA}")
    print(f"  Symbol:  {SYMBOL}")
    print(f"  Start:   {start_iso} UTC")
    print(f"  End:     {end_iso} UTC ('now')")
    print(f"  Now:     {now_utc.isoformat()}")
    print()

    print("Cost estimate ...")
    try:
        cost = float(client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            schema=SCHEMA,
            start=start_iso,
            end=end_iso,
            stype_in="continuous",
        ))
        print(f"  ${cost:.4f}")
    except Exception as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        print(f"\nDatabento Historical likely doesn't serve same-day data via this endpoint.")
        print(f"Recommend pivot to Option A (wait for IBKR funds + build other Phase 4 pieces).")
        return 2

    print("\nDownloading ...")
    try:
        data = client.timeseries.get_range(
            dataset=DATASET,
            symbols=[SYMBOL],
            schema=SCHEMA,
            start=start_iso,
            end=end_iso,
            stype_in="continuous",
        )
    except Exception as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 2

    df = data.to_df().reset_index()
    if df.empty:
        print("  Empty result. No same-day bars served.")
        return 2

    print(f"  Rows: {len(df)}")
    print(f"  First bar ts_event: {df['ts_event'].min()}")
    print(f"  Last bar ts_event:  {df['ts_event'].max()}")

    # Age of the latest bar
    latest_ts = df["ts_event"].max()
    if hasattr(latest_ts, "tz") and latest_ts.tz is None:
        latest_ts = latest_ts.tz_localize("UTC")
    elif hasattr(latest_ts, "to_pydatetime"):
        latest_ts = latest_ts.to_pydatetime()
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)

    age_seconds = (now_utc - latest_ts).total_seconds()
    age_minutes = age_seconds / 60.0

    print(f"\nLatest bar age: {age_seconds:.1f}s  ({age_minutes:.1f} min)")
    print(f"\nVerdict:")
    if age_minutes < 2:
        print("  >>> EXCELLENT: <2 min. Usable as live feed for ORB shakedown.")
        return 0
    elif age_minutes < 10:
        print("  >>> ACCEPTABLE: 2-10 min lag. Workable for ORB on 1m bars but cuts margin thin.")
        return 0
    elif age_minutes < 60:
        print("  >>> MARGINAL: 10-60 min lag. Strategy would miss most opening-range setups.")
        return 1
    else:
        print("  >>> UNUSABLE: >1h lag. Same-day data not served via Historical.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
