from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Position:
    symbol: str
    expiration: str
    strike: float
    option_type: str
    qty: int
    avg_price: float
    entry_value: float
    entry_time_utc: Optional[str] = None
    last_fill_time_utc: Optional[str] = None

    def key(self) -> str:
        return f"{self.symbol}|{self.expiration}|{self.strike:.2f}|{self.option_type}"


@dataclass
class ClosedTrade:
    symbol: str
    expiration: str
    strike: float
    option_type: str
    qty: int
    entry_price: float
    exit_price: float
    realized_pnl: float
    entry_time_utc: Optional[str] = None
    exit_time_utc: Optional[str] = None


@dataclass
class PositionLedger:
    contract_multiplier: int = 100
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    def apply_fill(self, payload: dict) -> None:
        order = payload.get("order_payload") or {}
        symbol = order.get("symbol")
        expiration = order.get("expiration")
        strike = order.get("strike")
        option_type = order.get("option_type") or order.get("direction")
        side = (order.get("side") or "BUY").upper()
        qty = int(payload.get("filled_qty") or order.get("qty") or order.get("contracts") or 0)
        fill_price = payload.get("fill_price")
        fill_time_utc = payload.get("fill_time_utc") or payload.get("execution_time_utc")

        if not symbol or expiration is None or strike is None or option_type is None:
            return
        if qty <= 0 or fill_price is None:
            return

        key = f"{symbol}|{expiration}|{float(strike):.2f}|{option_type}"
        if side == "BUY":
            pos = self.positions.get(key)
            if pos is None:
                pos = Position(
                    symbol=symbol,
                    expiration=expiration,
                    strike=float(strike),
                    option_type=option_type,
                    qty=0,
                    avg_price=0.0,
                    entry_value=0.0,
                )
                self.positions[key] = pos
            pos.entry_value += float(fill_price) * qty
            pos.qty += qty
            pos.avg_price = pos.entry_value / pos.qty
            if pos.entry_time_utc is None:
                pos.entry_time_utc = fill_time_utc
            pos.last_fill_time_utc = fill_time_utc
            return

        if side != "SELL":
            return

        pos = self.positions.get(key)
        if pos is None or pos.qty <= 0:
            return

        sell_qty = min(qty, pos.qty)
        pnl = (float(fill_price) - pos.avg_price) * sell_qty * self.contract_multiplier
        self.realized_pnl += pnl
        pos.entry_value -= pos.avg_price * sell_qty
        pos.qty -= sell_qty
        if pos.qty <= 0:
            self.closed_trades.append(
                ClosedTrade(
                    symbol=pos.symbol,
                    expiration=pos.expiration,
                    strike=pos.strike,
                    option_type=pos.option_type,
                    qty=sell_qty,
                    entry_price=pos.avg_price,
                    exit_price=float(fill_price),
                    realized_pnl=pnl,
                    entry_time_utc=pos.entry_time_utc,
                    exit_time_utc=fill_time_utc,
                )
            )
            self.positions.pop(key, None)
        else:
            pos.avg_price = pos.entry_value / pos.qty
            pos.last_fill_time_utc = fill_time_utc


def build_ledger_from_events(
    event_log_path: Path,
    session_date_exchange: Optional[str] = None,
    contract_multiplier: int = 100,
) -> PositionLedger:
    ledger = PositionLedger(contract_multiplier=contract_multiplier)
    if not event_log_path.exists():
        return ledger

    try:
        lines = event_log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ledger

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event_type") != "fill":
            continue
        record_session = record.get("session_date_exchange") or record.get("session_date_utc")
        if session_date_exchange and record_session and record_session != session_date_exchange:
            continue
        payload = record.get("payload", {})
        ledger.apply_fill(payload)
    return ledger


def parse_iso_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None
