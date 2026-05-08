from datetime import datetime, timezone

from src.positions import PositionLedger


def test_position_ledger_realized_pnl():
    ledger = PositionLedger(contract_multiplier=100)
    entry_time = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc).isoformat()
    exit_time = datetime(2024, 1, 2, 15, 5, tzinfo=timezone.utc).isoformat()
    buy_payload = {
        "filled_qty": 1,
        "fill_price": 1.0,
        "fill_time_utc": entry_time,
        "order_payload": {
            "symbol": "GLD",
            "expiration": "2024-01-03",
            "strike": 100.0,
            "option_type": "CALL",
            "side": "BUY",
            "qty": 1,
        },
    }
    sell_payload = {
        "filled_qty": 1,
        "fill_price": 1.5,
        "fill_time_utc": exit_time,
        "order_payload": {
            "symbol": "GLD",
            "expiration": "2024-01-03",
            "strike": 100.0,
            "option_type": "CALL",
            "side": "SELL",
            "qty": 1,
        },
    }

    ledger.apply_fill(buy_payload)
    assert len(ledger.positions) == 1
    ledger.apply_fill(sell_payload)
    assert len(ledger.positions) == 0
    assert ledger.realized_pnl == 50.0
