from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional


@dataclass
class DailyState:
    realized_pnl: float = 0.0
    last_loss_time_utc: Optional[str] = None


class DailyStateStore:
    def __init__(self, event_log_path: str, strict: bool = True) -> None:
        self._path = Path(event_log_path)
        self._strict = strict

    def load_today(self) -> DailyState:
        if not self._path.exists():
            return DailyState()
        realized_pnl = 0.0
        last_loss_time: Optional[datetime] = None
        today_utc = datetime.now(timezone.utc).date().isoformat()
        today_exchange = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_exchange = event.get("session_date_exchange")
                if session_exchange is not None:
                    if session_exchange != today_exchange:
                        continue
                else:
                    if event.get("session_date_utc") != today_utc:
                        continue
                if self._strict and event.get("event_type") not in ("pnl_update", "fill"):
                    continue
                payload = event.get("payload", {})
                pnl_value = self._extract_pnl(payload)
                if pnl_value is None:
                    continue
                realized_pnl += pnl_value
                if pnl_value < 0:
                    event_time = event.get("event_time_utc")
                    loss_dt = self._parse_time(event_time)
                    if loss_dt and (last_loss_time is None or loss_dt > last_loss_time):
                        last_loss_time = loss_dt

        return DailyState(
            realized_pnl=realized_pnl,
            last_loss_time_utc=last_loss_time.isoformat() if last_loss_time else None,
        )

    def _extract_pnl(self, payload: dict) -> Optional[float]:
        for key in ("realized_pnl", "pnl", "fill_pnl"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def _parse_time(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc)
        except ValueError:
            return None


class OrderStateStore:
    def __init__(self, event_log_path: str) -> None:
        self._path = Path(event_log_path)

    def load_open_orders(self, session_date_exchange: Optional[str] = None) -> dict[str, dict]:
        open_orders: dict[str, dict] = {}
        if not self._path.exists():
            return open_orders

        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return open_orders

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record_session = record.get("session_date_exchange") or record.get("session_date_utc")
            if session_date_exchange and record_session and record_session != session_date_exchange:
                continue
            event_type = record.get("event_type")
            payload = record.get("payload", {})
            order_id = payload.get("order_id")
            if not order_id:
                continue
            if event_type == "submit":
                open_orders[str(order_id)] = payload
            elif event_type in ("fill", "rejected", "cancel_confirm"):
                open_orders.pop(str(order_id), None)

        return open_orders
