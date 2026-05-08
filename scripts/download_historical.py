"""Download historical 1-minute bars for backtest validation.

Free-tier path: Massive (formerly Polygon.io). The aggregates endpoint
mirrors the legacy Polygon /v2/aggs/ticker/.../range/... contract; only
the host and key name differ. Set MASSIVE_API_KEY in your .env file.

Usage:
    python scripts/download_historical.py --start 2024-05-01 --end 2026-04-30

Outputs CSV to data/historical/{symbol}_1m.csv with columns:
    timestamp (UTC ISO8601), Open, High, Low, Close, Volume, vwap, transactions
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_dotenv  # noqa: E402
from src.rate_limit import RateLimiter  # noqa: E402


DEFAULT_BASE_URL = "https://api.massive.com"
DATA_DIR = ROOT / "data" / "historical"
DEFAULT_SYMBOLS = ("SPY", "QQQ", "GLD", "SLV", "NVDA", "AMZN")
CSV_FIELDS = ["timestamp", "Open", "High", "Low", "Close", "Volume", "vwap", "transactions"]


def fetch_aggregates(
    symbol: str,
    start: str,
    end: str,
    api_key: str,
    rate_limiter: RateLimiter,
    base_url: str = DEFAULT_BASE_URL,
    auth_mode: str = "query",
) -> list[dict]:
    """Fetch 1m aggregates for one symbol over [start, end].

    Pagination follows the response's next_url until exhausted. auth_mode
    is "query" (apiKey query param) or "header" (Authorization: Bearer).
    """
    rows: list[dict] = []
    base = base_url.rstrip("/")
    auth_mode = auth_mode.lower()

    initial_path = (
        f"/v2/aggs/ticker/{symbol}/range/1/minute/{start}/{end}"
        "?adjusted=true&sort=asc&limit=50000"
    )
    url: str | None = _attach_auth(f"{base}{initial_path}", api_key, auth_mode)
    headers = _auth_headers(api_key, auth_mode)

    while url:
        rate_limiter.acquire()
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Massive HTTP {exc.code} for {symbol}: {body[:300]}") from exc

        for r in payload.get("results") or []:
            rows.append(
                {
                    "timestamp": datetime.fromtimestamp(
                        r["t"] / 1000.0, tz=timezone.utc
                    ).isoformat(),
                    "Open": r.get("o"),
                    "High": r.get("h"),
                    "Low": r.get("l"),
                    "Close": r.get("c"),
                    "Volume": r.get("v"),
                    "vwap": r.get("vw"),
                    "transactions": r.get("n"),
                }
            )

        next_url = payload.get("next_url")
        url = _attach_auth(next_url, api_key, auth_mode) if next_url else None

    return rows


def _attach_auth(url: str, api_key: str, auth_mode: str) -> str:
    if auth_mode != "query":
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}apiKey={api_key}"


def _auth_headers(api_key: str, auth_mode: str) -> dict[str, str]:
    if auth_mode == "header":
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def write_csv(symbol: str, rows: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}_1m.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _validate_date(value: str, flag: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"ERROR: {flag} must be YYYY-MM-DD ({exc}).") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download historical 1m bars from Massive (formerly Polygon.io)"
    )
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"API base URL (default: ${{MASSIVE_BASE_URL:-{DEFAULT_BASE_URL}}})",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("query", "header"),
        default=None,
        help="API auth style; defaults to MASSIVE_AUTH_MODE or 'query'",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=5,
        help="API calls per minute (5 is the Massive free-tier ceiling)",
    )
    args = parser.parse_args()

    _validate_date(args.start, "--start")
    _validate_date(args.end, "--end")

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("MASSIVE_API_KEY")
    if not api_key:
        print(
            "ERROR: MASSIVE_API_KEY not set. Sign up at https://massive.com/ "
            "(free tier, no card required) and add the key to .env.",
            file=sys.stderr,
        )
        return 1

    base_url = args.base_url or os.environ.get("MASSIVE_BASE_URL") or DEFAULT_BASE_URL
    auth_mode = (args.auth_mode or os.environ.get("MASSIVE_AUTH_MODE") or "query").lower()

    limiter = RateLimiter(calls_per_window=args.rate_limit, window_seconds=60.0)

    failed = 0
    for symbol in args.symbols:
        print(f"[{symbol}] downloading {args.start} -> {args.end} ...", flush=True)
        try:
            rows = fetch_aggregates(
                symbol, args.start, args.end, api_key, limiter,
                base_url=base_url, auth_mode=auth_mode,
            )
        except Exception as exc:
            print(f"[{symbol}] FAILED: {exc}", file=sys.stderr)
            failed += 1
            continue
        if not rows:
            print(f"[{symbol}] no rows returned (date range outside free-tier window?)")
            continue
        path = write_csv(symbol, rows, args.out_dir)
        print(f"[{symbol}] wrote {len(rows):,} rows -> {path}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
