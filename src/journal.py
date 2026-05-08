import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import PlanResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exchange_session_date() -> str:
    eastern = ZoneInfo("America/New_York")
    return datetime.now(eastern).date().isoformat()


def _config_hash(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EventJournal:
    def __init__(self, config: dict) -> None:
        logging_cfg = config.get("logging", {})
        self._path = Path(logging_cfg.get("event_log_path", logging_cfg.get("journal_path", "journal.jsonl")))
        self._config_hash = _config_hash(config)
        self._config_version = config.get("config_version")
        self._change_notes = config.get("change_notes")

    def log_event(self, event_type: str, payload: dict) -> None:
        record = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "event_time_utc": _utc_now_iso(),
            "session_date_utc": datetime.now(timezone.utc).date().isoformat(),
            "session_date_exchange": _exchange_session_date(),
            "config_hash": self._config_hash,
            "config_version": self._config_version,
            "change_notes": self._change_notes,
            "payload": payload,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def log_plan(plan: PlanResult, journal_path: str) -> None:
    path = Path(journal_path)
    payload = plan.to_dict()
    payload["event_type"] = "plan"
    payload["event_time_utc"] = _utc_now_iso()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
