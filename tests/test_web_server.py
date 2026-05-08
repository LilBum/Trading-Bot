import json
from pathlib import Path

import pandas as pd

from src.web_server import _compute_session_stats, _compute_performance_stats


def test_compute_session_totals_counts_plans(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session_date = "2026-02-06"
    records = [
        {
            "event_type": "plan",
            "session_date_exchange": session_date,
            "payload": {"status": "ALLOWED"},
        },
        {
            "event_type": "plan",
            "session_date_exchange": session_date,
            "payload": {"status": "REJECTED"},
        },
        {
            "event_type": "plan",
            "session_date_exchange": "2026-02-05",
            "payload": {"status": "ALLOWED"},
        },
        {"event_type": "error", "payload": {}},
    ]
    log_path.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")

    totals, accepts = _compute_session_stats(Path(log_path), session_date_exchange=session_date)
    assert totals["total"] == 2
    assert totals["allowed"] == 1
    assert totals["rejected"] == 1
    assert len(accepts) == 1


class _StubProvider:
    def __init__(self, chain_df: pd.DataFrame) -> None:
        self._chain = chain_df

    def get_options_chain(self, symbol: str, target_dte: int):
        return symbol, self._chain


def test_compute_performance_stats_from_fills(tmp_path):
    log_path = tmp_path / "events.jsonl"
    session_date = "2026-02-07"
    record = {
        "event_type": "fill",
        "session_date_exchange": session_date,
        "payload": {
            "filled_qty": 2,
            "fill_price": 1.0,
            "fill_time_utc": "2026-02-07T15:00:00+00:00",
            "order_payload": {
                "symbol": "GLD",
                "expiration": "2026-02-07",
                "strike": 200.0,
                "option_type": "CALL",
                "qty": 2,
                "side": "BUY",
            },
        },
    }
    log_path.write_text(json.dumps(record), encoding="utf-8")
    chain = pd.DataFrame(
        [
            {
                "expiration": "2026-02-07",
                "strike": 200.0,
                "option_type": "CALL",
                "bid": 1.2,
                "ask": 1.4,
                "last_price": 1.3,
            }
        ]
    )
    provider = _StubProvider(chain)
    config = {"options": {"target_dte": 1}, "execution": {"paper": {"contract_multiplier": 100}}}

    perf = _compute_performance_stats(Path(log_path), provider, config, session_date_exchange=session_date)
    assert perf["totals"]["open_positions"] == 1
    assert perf["totals"]["invested"] == 200.0
    assert perf["totals"]["market_value"] == 260.0
