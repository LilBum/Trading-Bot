from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from .engine import PlannerApp
from .positions import build_ledger_from_events


@dataclass
class ReplayConfig:
    bars_path: Path
    chain_path: Optional[Path]
    symbol: str
    limit: Optional[int] = None


class ReplayMarketDataProvider:
    def __init__(self, bars: pd.DataFrame, chain: Optional[pd.DataFrame]) -> None:
        self._bars = bars
        self._chain = chain
        self._current_idx = len(bars) - 1
        self._healthy = True

    def set_index(self, idx: int) -> None:
        self._current_idx = max(0, min(idx, len(self._bars) - 1))

    def get_intraday_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        return self._bars.iloc[: self._current_idx + 1].copy()

    def get_options_chain(self, symbol: str, target_dte: int) -> tuple[str, pd.DataFrame] | None:
        if self._chain is None or self._chain.empty:
            return None
        expiration = str(self._chain["expiration"].iloc[0])
        return expiration, self._chain.copy()

    def health_check(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return None


def run_replay(config: dict, replay_cfg: ReplayConfig) -> dict:
    bars = _load_bars(replay_cfg.bars_path)
    chain = _load_chain(replay_cfg.chain_path) if replay_cfg.chain_path else None
    provider = ReplayMarketDataProvider(bars, chain)
    app = PlannerApp(config)
    app.provider = provider
    app.provider_name = "replay"
    app._fallback_enabled = False
    app.config["strategy"]["symbols"] = [replay_cfg.symbol]
    limit = replay_cfg.limit or len(bars)
    start_idx = max(1, len(bars) - limit)
    for idx in range(start_idx, len(bars)):
        provider.set_index(idx)
        app.run(log_to_journal=True)
    return _summarize_events(config)


def _summarize_events(config: dict) -> dict:
    event_log_path = Path(
        config.get("logging", {}).get("event_log_path", "events.jsonl")
    )
    contract_multiplier = (
        config.get("execution", {}).get("paper", {}).get("contract_multiplier", 100) or 100
    )
    ledger = build_ledger_from_events(
        event_log_path,
        session_date_exchange=None,
        contract_multiplier=contract_multiplier,
    )
    gross_win = sum(t.realized_pnl for t in ledger.closed_trades if t.realized_pnl > 0)
    gross_loss = sum(-t.realized_pnl for t in ledger.closed_trades if t.realized_pnl < 0)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else None
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(ledger.closed_trades, key=lambda t: t.exit_time_utc or ""):
        equity += trade.realized_pnl
        if equity > peak:
            peak = equity
        max_dd = max(max_dd, peak - equity)
    wins = len([t for t in ledger.closed_trades if t.realized_pnl > 0])
    win_rate = (wins / len(ledger.closed_trades) * 100.0) if ledger.closed_trades else 0.0
    return {
        "realized_pnl": round(ledger.realized_pnl, 2),
        "trades": len(ledger.closed_trades),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "max_drawdown": round(max_dd, 2),
    }


def _load_bars(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts_col = None
    for candidate in ("timestamp", "Datetime", "Date", "time"):
        if candidate in df.columns:
            ts_col = candidate
            break
    if ts_col is None:
        raise ValueError("Bars file missing timestamp column.")
    df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
    df = df.dropna(subset=[ts_col])
    df = df.rename(columns={ts_col: "timestamp"})
    df = df.set_index("timestamp")
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Bars file missing columns: {', '.join(sorted(missing))}")
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _load_chain(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"expiration", "strike", "option_type"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Chain file missing columns: {', '.join(sorted(missing))}")
    return df
