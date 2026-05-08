from __future__ import annotations

from .execution_adapter import (
    NullExecutionAdapter,
    PaperExecutionAdapter,
    TradierExecutionAdapter,
    WebullExecutionAdapter,
)
from .journal import EventJournal


def create_execution_adapter(config: dict, journal: EventJournal):
    exec_cfg = config.get("execution", {})
    adapter = (exec_cfg.get("adapter") or "null").lower()
    if adapter == "webull":
        return WebullExecutionAdapter(config, journal)
    if adapter == "tradier":
        return TradierExecutionAdapter(config, journal)
    if adapter == "paper":
        return PaperExecutionAdapter(config, journal)
    return NullExecutionAdapter(journal)
