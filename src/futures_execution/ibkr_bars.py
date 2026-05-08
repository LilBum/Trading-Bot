"""IBKR-backed `BarsProvider` for the live runner.

Pulls 1-minute OHLCV bars from `IB.reqHistoricalData()` for the current
trading session, returning them ET-indexed in the same shape the backtest
session loader produces (so the signal engine doesn't care which path
the bars came from).

The `useRTH=False` flag is intentional. The driving loop wakes at 08:00 ET
to pre-warm the bar buffer, but the ORB engine itself anchors on the
09:30 ET cash open (config.json `orb.session_open_time` = "09:30"). Bars
between 08:00 and 09:30 form the warmup window so the signal engine has
enough context to evaluate the opening range as it forms after 09:30.
RTH-only bars would miss this pre-cash warmup entirely.

Without a CME real-time subscription this still works but returns delayed
bars (~10-15 min lag), which is fine for development but fatal for live
trading. The `IBKRFuturesQuoteProvider` will populate quotes once the
subscription is active; this bars provider will follow.
"""

from __future__ import annotations

import asyncio as _asyncio
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.futures_execution.ibkr import IBKR_CONTRACT_SPEC

try:
    _asyncio.get_event_loop()
except RuntimeError:
    _asyncio.set_event_loop(_asyncio.new_event_loop())

try:  # pragma: no cover - optional runtime dep
    from ib_insync import ContFuture
except ImportError:  # pragma: no cover
    ContFuture = None


EASTERN = ZoneInfo("America/New_York")
DEFAULT_SESSION_START = time(8, 0)


class IBKRBarsProvider:
    """Implements the BarsProvider Protocol via IB.reqHistoricalData."""

    def __init__(
        self,
        ib_client,
        session_start_et: time = DEFAULT_SESSION_START,
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        now_fn=None,
    ) -> None:
        self._ib = ib_client
        self._session_start_et = session_start_et
        self._bar_size = bar_size
        self._what_to_show = what_to_show
        self._use_rth = use_rth
        self._contract_cache: dict = {}
        self._now_fn = now_fn or (lambda: datetime.now(EASTERN))

    def get_session_bars(self, symbol: str) -> pd.DataFrame:
        """Return ET-indexed OHLCV bars from session_start_et to now."""
        contract = self._resolve_contract(symbol)
        duration_str = self._duration_from_session_start()
        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration_str,
            barSizeSetting=self._bar_size,
            whatToShow=self._what_to_show,
            useRTH=self._use_rth,
            formatDate=1,
        )
        if not bars:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return self._bars_to_dataframe(bars)

    # ----- helpers ----------------------------------------------------------

    def _resolve_contract(self, symbol: str):
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]
        if ContFuture is None:
            raise RuntimeError("ib_insync not installed; cannot resolve IBKR contracts.")
        spec = IBKR_CONTRACT_SPEC.get(symbol)
        if spec is None:
            raise ValueError(f"Unknown IBKR futures symbol: {symbol}")
        contract = ContFuture(symbol, exchange=spec["exchange"], currency=spec["currency"])
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify ContFuture for {symbol}")
        self._contract_cache[symbol] = qualified[0]
        return qualified[0]

    def _duration_from_session_start(self) -> str:
        """Compute IBKR durationStr from the configured session start to now.

        Uses seconds-resolution and pads by 60 seconds to make sure the bar
        for the current minute is included (IBKR sometimes omits the open
        boundary if duration is computed too tightly).
        """
        now_et = self._now_fn()
        if now_et.tzinfo is None:
            now_et = now_et.replace(tzinfo=EASTERN)
        session_start_et = now_et.replace(
            hour=self._session_start_et.hour,
            minute=self._session_start_et.minute,
            second=0,
            microsecond=0,
        )
        if now_et < session_start_et:
            # Pre-session: pull a token amount so the runner gets a non-empty
            # response; warmup gate will keep it from trading.
            return "60 S"
        elapsed_seconds = (now_et - session_start_et).total_seconds() + 60
        elapsed_seconds = int(max(60, min(elapsed_seconds, 86400)))  # clamp to [1m, 24h]
        return f"{elapsed_seconds} S"

    def _bars_to_dataframe(self, bars) -> pd.DataFrame:
        """Convert ib_insync BarData list to an ET-indexed OHLCV DataFrame."""
        rows = []
        for b in bars:
            ts = b.date
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    # IB returns naive timestamps in the timezone configured in
                    # TWS settings (we set "instrument timezone"). Treat as UTC
                    # to be safe; ET conversion happens below.
                    ts = ts.replace(tzinfo=ZoneInfo("UTC"))
                ts_et = ts.astimezone(EASTERN)
            else:
                # IB sometimes returns date-only for daily bars; not applicable here
                # but defend anyway.
                ts_et = pd.to_datetime(str(ts), utc=True).tz_convert(EASTERN)
            rows.append({
                "ts": ts_et,
                "Open": float(b.open),
                "High": float(b.high),
                "Low": float(b.low),
                "Close": float(b.close),
                "Volume": float(b.volume) if b.volume is not None else 0.0,
            })
        df = pd.DataFrame(rows)
        df = df.set_index("ts").sort_index()
        # Filter to session start onwards, in ET.
        if not df.empty:
            now_et = self._now_fn()
            if now_et.tzinfo is None:
                now_et = now_et.replace(tzinfo=EASTERN)
            session_start = now_et.replace(
                hour=self._session_start_et.hour,
                minute=self._session_start_et.minute,
                second=0,
                microsecond=0,
            )
            df = df[df.index >= session_start]
        return df[["Open", "High", "Low", "Close", "Volume"]]
