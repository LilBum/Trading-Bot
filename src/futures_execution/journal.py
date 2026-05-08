"""Append-only JSONL journal of LivePaperRunner iterations.

Every `IterationResult` from the live runner gets written as one line of
JSONL. After a session, the journal file is the audit trail for the morning
report: every signal evaluation, every fill, every reject, every hold.

Format note: timestamps stored as ISO 8601 strings, FuturesOrderAck flattened
into a nested object. JSONL means: one valid JSON object per line, parseable
with `json.loads(line)` in any language.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.futures_execution.live_runner import IterationResult


def _to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, datetimes, and pandas types to JSON-safe."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return str(value)


def serialize_iteration_result(result: IterationResult) -> dict:
    """Flatten an IterationResult into a JSON-serializable dict."""
    return {
        "timestamp_et": _to_jsonable(result.timestamp_et),
        "bars_count": result.bars_count,
        "signal_direction": result.signal_direction,
        "action": result.action,
        "order_ack": _to_jsonable(result.order_ack),
        "note": result.note,
    }


class IterationJournal:
    """Append-only JSONL journal. Survives process restart trivially."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, result: IterationResult) -> None:
        record = serialize_iteration_result(result)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def append_error(self, timestamp_et: datetime, error_message: str) -> None:
        """Record a runner exception as a journal entry."""
        record = {
            "timestamp_et": _to_jsonable(timestamp_et),
            "bars_count": 0,
            "signal_direction": None,
            "action": "exception",
            "order_ack": None,
            "note": error_message,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
