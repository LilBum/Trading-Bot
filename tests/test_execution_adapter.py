from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.execution_adapter as execution_adapter
from src.execution_adapter import (
    PaperExecutionAdapter,
    WebullExecutionAdapter,
    analyze_webull_trade_history,
    fetch_webull_trade_history,
    list_webull_paper_accounts,
)
from src.journal import EventJournal


def _base_config(tmp_path: Path) -> dict:
    return {
        "execution": {
            "mode": "paper",
            "paper": {"fill_policy": "mid", "slippage_bps": 0},
            "webull": {"simulate_if_unavailable": True},
        },
        "logging": {"event_log_path": str(tmp_path / "events.jsonl")},
    }


def test_paper_execution_adapter_fills_mid(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    journal = EventJournal(config)
    adapter = PaperExecutionAdapter(config, journal)

    payload = {"side": "BUY", "qty": 2, "bid": 1.0, "ask": 1.2, "mid": 1.1}
    result = adapter.submit_order(payload)

    assert result["status"] == "filled"
    assert result["filled_qty"] == 2
    assert result["fill_price"] == 1.1

    lines = Path(config["logging"]["event_log_path"]).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert any(event["event_type"] == "fill" for event in events)


def test_webull_execution_adapter_falls_back_to_paper(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    journal = EventJournal(config)
    adapter = WebullExecutionAdapter(config, journal)

    payload = {"side": "BUY", "qty": 1, "bid": 2.0, "ask": 2.2, "mid": 2.1}
    result = adapter.submit_order(payload)

    assert result["status"] == "filled"
    assert result["fill_price"] == 2.1


def test_list_webull_paper_accounts_requires_creds(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config["webull"] = {"app_key": "", "app_secret": "", "test_app_key": "", "test_app_secret": ""}
    with pytest.raises(RuntimeError):
        list_webull_paper_accounts(config)


def test_fetch_webull_trade_history_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def json(self) -> dict:
            return self._payload

    class _FakeApiClient:
        def __init__(self, app_key: str, app_secret: str, region: str) -> None:
            self.app_key = app_key
            self.app_secret = app_secret
            self.region = region
            self.endpoints: list[tuple[str, str]] = []

        def add_endpoint(self, region: str, endpoint: str) -> None:
            self.endpoints.append((region, endpoint))

    pages = [
        {
            "data": {
                "orders": [
                    {"order_id": "1", "client_order_id": "c1", "symbol": "AAPL", "status": "FILLED"},
                    {"order_id": "2", "client_order_id": "c2", "symbol": "MSFT", "status": "NEW"},
                ],
                "last_client_order_id": "c2",
                "last_order_id": "2",
            }
        },
        {
            "data": {
                "orders": [
                    {"order_id": "3", "client_order_id": "c3", "symbol": "AAPL", "status": "FILLED"},
                ]
            }
        },
    ]
    calls: list[dict] = []

    class _FakeTradeClient:
        def __init__(self, _api_client: _FakeApiClient) -> None:
            self.order_v2 = self

        def get_order_history(
            self,
            account_id: str,
            page_size: int | None = None,
            start_date: str | None = None,
            end_date: str | None = None,
            last_client_order_id: str | None = None,
            last_order_id: str | None = None,
        ) -> _FakeResponse:
            calls.append(
                {
                    "account_id": account_id,
                    "page_size": page_size,
                    "start_date": start_date,
                    "end_date": end_date,
                    "last_client_order_id": last_client_order_id,
                    "last_order_id": last_order_id,
                }
            )
            idx = len(calls) - 1
            payload = pages[idx] if idx < len(pages) else {"data": {"orders": []}}
            return _FakeResponse(payload, status_code=200)

    monkeypatch.setattr(execution_adapter, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(execution_adapter, "TradeClient", _FakeTradeClient)

    config = {
        "webull": {"test_app_key": "k", "test_app_secret": "s", "region": "us"},
        "execution": {"webull": {"account_id": "ACC123", "paper_endpoint": "https://paper.webull.test"}},
    }
    result = fetch_webull_trade_history(
        config,
        mode="paper",
        start_date="2026-01-01",
        end_date="2026-01-31",
        page_size=2,
        max_pages=5,
    )

    assert result["order_count"] == 3
    assert result["pages_fetched"] == 2
    assert result["open_order_ids"] == ["c2"]
    assert calls[0]["last_order_id"] is None
    assert calls[1]["last_order_id"] == "2"
    assert calls[1]["last_client_order_id"] == "c2"


def test_analyze_webull_trade_history_aggregates_metrics() -> None:
    history = {
        "account_id": "ACC123",
        "mode": "paper",
        "endpoint": "https://paper.webull.test",
        "query": {"start_date": "2026-02-01", "end_date": "2026-02-03"},
        "pages_fetched": 1,
        "status_codes": [200],
        "orders": [
            {
                "order_id": "1",
                "client_order_id": "a1",
                "symbol": "AAPL",
                "status": "FILLED",
                "side": "BUY",
                "filled_quantity": 2,
                "avg_fill_price": 10.0,
                "filled_time": "2026-02-01T10:00:00Z",
            },
            {
                "order_id": "2",
                "client_order_id": "a2",
                "symbol": "AAPL",
                "status": "FILLED",
                "side": "SELL",
                "filled_quantity": 1,
                "avg_fill_price": 12.0,
                "realized_pnl": 50.0,
                "filled_time": "2026-02-02T10:00:00Z",
            },
            {
                "order_id": "3",
                "client_order_id": "a3",
                "symbol": "TSLA",
                "status": "REJECTED",
                "side": "BUY",
                "create_time": "2026-02-03T10:00:00Z",
            },
        ],
    }

    analysis = analyze_webull_trade_history(history)
    summary = analysis["summary"]

    assert summary["orders_total"] == 3
    assert summary["filled_orders"] == 2
    assert summary["rejected_orders"] == 1
    assert summary["gross_buy_notional"] == 20.0
    assert summary["gross_sell_notional"] == 12.0
    assert summary["net_cash_flow"] == -8.0
    assert summary["realized_pnl"] == 50.0
    assert summary["unique_symbols"] == 2

    by_symbol = {row["symbol"]: row for row in analysis["by_symbol"]}
    assert by_symbol["AAPL"]["orders"] == 2
    assert by_symbol["AAPL"]["filled_orders"] == 2
    assert by_symbol["AAPL"]["net_qty"] == 1
