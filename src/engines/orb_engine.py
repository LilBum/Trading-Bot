"""Adapter: expose `compute_orb_info` as the SessionRunner-compatible signal engine.

Maps `compute_orb_info` status -> direction:
    'confirmed' + direction CALL/PUT -> matching direction with empty reject_reasons
    everything else                  -> 'NONE' with status as a reject reason

Note: the existing ORB module returns underlying-points stops (entry +/- N).
This adapter does NOT translate them — the runner keeps using premium-%
take-profit / stop-loss from config.exits. That trades fidelity for speed
in the quick-pass evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from src.orb import compute_orb_info


@dataclass
class _OrbSignal:
    """Minimal SignalDecision-shaped object for the SessionRunner."""

    symbol: str
    direction: str  # "CALL" | "PUT" | "NONE"
    reject_reasons: list[str]
    bar_timestamp: datetime
    decision_time_utc: str = ""
    setup: str = "Opening Range Breakout"
    entry_trigger: str = ""
    invalidation: str = ""
    premium_stop: str = ""
    targets: str = ""
    regime_info: str = ""
    atr_value: float | None = None
    atr_pct: float | None = None
    higher_timeframe_trend: str | None = None
    sentiment_value: float | None = None
    sentiment_label: str | None = None
    sentiment_source: str | None = None
    warnings: list[str] = field(default_factory=list)


class OrbSignalEngine:
    """Wraps compute_orb_info to be used as a SessionRunner signal engine."""

    def __init__(self, orb_cfg: dict) -> None:
        # Force-enable in our local copy: the runner controls when we're
        # called, and a config-level disable would be a foot-gun here.
        self._cfg = {**(orb_cfg or {}), "enabled": True}

    @property
    def range_minutes(self) -> int:
        return int(self._cfg.get("range_minutes", 15))

    def evaluate(self, symbol: str, df: pd.DataFrame, runtime_config: dict) -> _OrbSignal:
        if len(df) == 0:
            return _OrbSignal(
                symbol=symbol,
                direction="NONE",
                reject_reasons=["empty bars"],
                bar_timestamp=datetime.now(timezone.utc),
                decision_time_utc=datetime.now(timezone.utc).isoformat(),
            )

        timestamp = df.index[-1].to_pydatetime()
        decision_time = datetime.now(timezone.utc).isoformat()

        info = compute_orb_info(symbol, df, {"orb": self._cfg})
        if info is None:
            return _OrbSignal(
                symbol=symbol,
                direction="NONE",
                reject_reasons=["ORB disabled"],
                bar_timestamp=timestamp,
                decision_time_utc=decision_time,
            )

        status = str(info.get("status", "unknown"))
        if status == "confirmed":
            direction = str(info.get("direction") or "NONE").upper()
            confirm_reason = info.get("confirm_reason") or "breakout"
            range_high = info.get("range_high")
            range_low = info.get("range_low")
            return _OrbSignal(
                symbol=symbol,
                direction=direction if direction in ("CALL", "PUT") else "NONE",
                reject_reasons=[] if direction in ("CALL", "PUT") else [f"unknown direction: {direction}"],
                bar_timestamp=timestamp,
                decision_time_utc=decision_time,
                regime_info=(
                    f"ORB {confirm_reason} "
                    f"(range {range_low}-{range_high}, {self.range_minutes}m window)"
                ),
            )

        return _OrbSignal(
            symbol=symbol,
            direction="NONE",
            reject_reasons=[f"ORB status: {status}"],
            bar_timestamp=timestamp,
            decision_time_utc=decision_time,
        )
