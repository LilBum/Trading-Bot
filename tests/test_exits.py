from pathlib import Path
import json

import pandas as pd

from src.exits import ExitManager
from src.journal import EventJournal


class StubProvider:
    def __init__(self, chain: pd.DataFrame) -> None:
        self._chain = chain

    def get_options_chain(self, symbol: str, target_dte: int):
        return "2024-01-03", self._chain


def test_exit_manager_triggers_take_profit(tmp_path: Path):
    event_log = tmp_path / "events.jsonl"
    fill_event = {
        "event_type": "fill",
        "session_date_exchange": "2024-01-02",
        "payload": {
            "filled_qty": 1,
            "fill_price": 1.0,
            "fill_time_utc": "2024-01-02T15:00:00+00:00",
            "order_payload": {
                "symbol": "GLD",
                "expiration": "2024-01-03",
                "strike": 100.0,
                "option_type": "CALL",
                "side": "BUY",
                "qty": 1,
            },
        },
    }
    event_log.write_text(json.dumps(fill_event) + "\n", encoding="utf-8")

    chain = pd.DataFrame(
        [
            {
                "expiration": "2024-01-03",
                "strike": 100.0,
                "option_type": "CALL",
                "bid": 1.4,
                "ask": 1.6,
                "last_price": 1.5,
            }
        ]
    )
    config = {
        "logging": {"event_log_path": str(event_log)},
        "options": {"target_dte": 1},
        "execution": {"enabled": False, "auto_submit": False},
        "exits": {"take_profit_pct": 0.3},
    }
    manager = ExitManager(config, StubProvider(chain), None, EventJournal(config))
    decisions = manager.evaluate_and_submit(session_date_exchange="2024-01-02")
    assert decisions
    assert decisions[0].reason == "take_profit"


def test_exit_manager_triggers_trailing_stop(tmp_path: Path):
    event_log = tmp_path / "events.jsonl"
    state_path = tmp_path / "positions_state.json"
    fill_event = {
        "event_type": "fill",
        "session_date_exchange": "2024-01-02",
        "payload": {
            "filled_qty": 1,
            "fill_price": 1.0,
            "fill_time_utc": "2024-01-02T15:00:00+00:00",
            "order_payload": {
                "symbol": "GLD",
                "expiration": "2024-01-03",
                "strike": 100.0,
                "option_type": "CALL",
                "side": "BUY",
                "qty": 1,
            },
        },
    }
    event_log.write_text(json.dumps(fill_event) + "\n", encoding="utf-8")

    chain_up = pd.DataFrame(
        [
            {
                "expiration": "2024-01-03",
                "strike": 100.0,
                "option_type": "CALL",
                "bid": 1.1,
                "ask": 1.3,
                "last_price": 1.2,
            }
        ]
    )
    chain_down = pd.DataFrame(
        [
            {
                "expiration": "2024-01-03",
                "strike": 100.0,
                "option_type": "CALL",
                "bid": 0.9,
                "ask": 1.1,
                "last_price": 1.0,
            }
        ]
    )
    config = {
        "logging": {"event_log_path": str(event_log), "positions_state_path": str(state_path)},
        "options": {"target_dte": 1},
        "execution": {"enabled": False, "auto_submit": False},
        "exits": {"trailing_stop_pct": 0.1, "trailing_stop_activation_pct": 0.0},
    }

    manager = ExitManager(config, StubProvider(chain_up), None, EventJournal(config))
    decisions = manager.evaluate_and_submit(session_date_exchange="2024-01-02")
    assert not decisions

    manager = ExitManager(config, StubProvider(chain_down), None, EventJournal(config))
    decisions = manager.evaluate_and_submit(session_date_exchange="2024-01-02")
    assert decisions
    assert decisions[0].reason == "trailing_stop"
