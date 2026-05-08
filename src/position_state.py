from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class PositionState:
    peak_mid: float
    trailing_active: bool
    last_update_utc: Optional[str] = None


@dataclass
class PositionStateStore:
    path: Path
    state: dict[str, PositionState] = field(default_factory=dict)

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            peak = value.get("peak_mid")
            trailing = value.get("trailing_active", False)
            if peak is None:
                continue
            try:
                peak_val = float(peak)
            except (TypeError, ValueError):
                continue
            self.state[key] = PositionState(
                peak_mid=peak_val,
                trailing_active=bool(trailing),
                last_update_utc=value.get("last_update_utc"),
            )

    def save(self) -> None:
        payload = {
            key: {
                "peak_mid": value.peak_mid,
                "trailing_active": value.trailing_active,
                "last_update_utc": value.last_update_utc,
            }
            for key, value in self.state.items()
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def update(
        self,
        key: str,
        current_mid: float,
        entry_price: float,
        activation_pct: Optional[float],
    ) -> PositionState:
        now = datetime.now(timezone.utc).isoformat()
        state = self.state.get(key)
        if state is None:
            state = PositionState(peak_mid=current_mid, trailing_active=False, last_update_utc=now)
            self.state[key] = state
        if current_mid > state.peak_mid:
            state.peak_mid = current_mid
        if activation_pct is None:
            state.trailing_active = True
        else:
            try:
                activation = float(activation_pct)
            except (TypeError, ValueError):
                activation = None
            if activation is None:
                state.trailing_active = True
            elif current_mid >= entry_price * (1.0 + activation):
                state.trailing_active = True
        state.last_update_utc = now
        return state

    def prune(self, active_keys: set[str]) -> None:
        for key in list(self.state.keys()):
            if key not in active_keys:
                self.state.pop(key, None)
