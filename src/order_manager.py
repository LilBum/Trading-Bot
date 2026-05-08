from __future__ import annotations

from .execution_adapter import ExecutionAdapter
from .journal import EventJournal


class OrderManager:
    def __init__(self, execution_adapter: ExecutionAdapter, journal: EventJournal) -> None:
        self.execution_adapter = execution_adapter
        self.journal = journal

    def submit_bracket_intent(self, order_intent: dict, log_intent: bool = True) -> dict:
        if log_intent:
            self.journal.log_event("order_intent", order_intent)
        return self.execution_adapter.submit_order(order_intent)

    def cancel_all(self) -> None:
        self.journal.log_event("cancel_request", {"scope": "all"})
        self.journal.log_event("cancel_confirm", {"scope": "all", "status": "no_adapter"})
