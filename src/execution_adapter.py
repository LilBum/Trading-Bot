from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import time
import uuid

import requests

from .journal import EventJournal

try:
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
except ImportError:  # pragma: no cover - optional dependency
    ApiClient = None
    TradeClient = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionResponse:
    status: str
    order_id: str | None = None
    filled_qty: int | None = None
    fill_price: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "filled_qty": self.filled_qty,
            "fill_price": self.fill_price,
            "reason": self.reason,
        }


class ExecutionAdapter:
    def submit_order(self, order_payload: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def reconcile(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class NullExecutionAdapter(ExecutionAdapter):
    def __init__(self, journal: EventJournal) -> None:
        self.journal = journal

    def submit_order(self, order_payload: dict) -> dict:
        self.journal.log_event(
            "submit",
            {
                "order_payload": order_payload,
                "status": "not_submitted",
            },
        )
        self.journal.log_event(
            "ack",
            {
                "order_payload": order_payload,
                "status": "not_acknowledged",
            },
        )
        return ExecutionResponse(status="not_submitted", reason="execution adapter not configured").to_dict()

    def cancel_order(self, order_id: str) -> dict:
        self.journal.log_event(
            "cancel_request",
            {
                "order_id": order_id,
            },
        )
        self.journal.log_event(
            "cancel_confirm",
            {
                "order_id": order_id,
                "status": "not_cancelled",
            },
        )
        return ExecutionResponse(status="not_cancelled", reason="execution adapter not configured").to_dict()

    def reconcile(self) -> dict:
        self.journal.log_event("final_reconciliation", {"status": "no_adapter"})
        return {"status": "no_adapter"}


class PaperExecutionAdapter(ExecutionAdapter):
    def __init__(self, config: dict, journal: EventJournal) -> None:
        self.config = config
        self.journal = journal
        self.paper_cfg = config.get("execution", {}).get("paper", {})

    def submit_order(self, order_payload: dict) -> dict:
        order_id = str(uuid.uuid4())
        side = (order_payload.get("side") or "BUY").upper()
        qty = int(order_payload.get("qty") or order_payload.get("contracts") or 0)
        order_type = (order_payload.get("order_type") or "LIMIT").upper()
        strict_limits = bool(self.paper_cfg.get("strict_limits", False))
        fill_price = self._compute_fill_price(order_payload, side)
        status = "filled" if qty > 0 and fill_price is not None else "rejected"
        reason = None if status == "filled" else "invalid_quantity_or_price"
        if status == "filled" and strict_limits and order_type == "LIMIT":
            limit = order_payload.get("limit_price")
            bid = order_payload.get("bid")
            ask = order_payload.get("ask")
            marketable = True
            if side == "BUY" and ask is not None and limit is not None:
                marketable = float(limit) >= float(ask)
            elif side == "SELL" and bid is not None and limit is not None:
                marketable = float(limit) <= float(bid)
            if not marketable:
                status = "rejected"
                reason = "limit_not_marketable"
                fill_price = None
        fill_pnl = None
        fill_slippage = None
        if status == "filled":
            fill_pnl = self._compute_fill_pnl(order_payload, side, qty, fill_price)
            reference = order_payload.get("mid") or order_payload.get("limit_price")
            if reference is not None:
                try:
                    ref_val = float(reference)
                    if side == "BUY":
                        fill_slippage = float(fill_price) - ref_val
                    else:
                        fill_slippage = ref_val - float(fill_price)
                except (TypeError, ValueError):
                    fill_slippage = None

        self.journal.log_event(
            "submit",
            {
                "order_id": order_id,
                "status": "submitted",
                "submission_time_utc": _utc_now_iso(),
                "order_payload": order_payload,
                "adapter": "paper",
                "mode": "paper",
            },
        )
        self.journal.log_event(
            "ack",
            {
                "order_id": order_id,
                "status": "acknowledged",
                "ack_time_utc": _utc_now_iso(),
                "adapter": "paper",
                "mode": "paper",
            },
        )

        delay_ms = int(self.paper_cfg.get("fill_delay_ms", 0) or 0)
        if delay_ms > 0:
            time.sleep(min(delay_ms, 5000) / 1000.0)

        if status == "filled":
            self.journal.log_event(
                "fill",
                {
                    "order_id": order_id,
                    "status": "filled",
                    "filled_qty": qty,
                    "fill_price": round(fill_price, 4) if fill_price is not None else None,
                    "fill_time_utc": _utc_now_iso(),
                    "fill_pnl": fill_pnl,
                    "fill_slippage": round(fill_slippage, 4) if fill_slippage is not None else None,
                    "order_payload": order_payload,
                    "adapter": "paper",
                    "mode": "paper",
                },
            )
        else:
            self.journal.log_event(
                "rejected",
                {
                    "order_id": order_id,
                    "status": "rejected",
                    "reason": reason,
                    "adapter": "paper",
                    "mode": "paper",
                },
            )

        return ExecutionResponse(
            status=status,
            order_id=order_id,
            filled_qty=qty if status == "filled" else 0,
            fill_price=round(fill_price, 4) if fill_price is not None else None,
            reason=reason,
        ).to_dict()

    def cancel_order(self, order_id: str) -> dict:
        self.journal.log_event(
            "cancel_request",
            {
                "order_id": order_id,
                "status": "cancel_requested",
                "adapter": "paper",
                "mode": "paper",
            },
        )
        self.journal.log_event(
            "cancel_confirm",
            {
                "order_id": order_id,
                "status": "cancelled",
                "adapter": "paper",
                "mode": "paper",
            },
        )
        return ExecutionResponse(status="cancelled", order_id=order_id).to_dict()

    def reconcile(self) -> dict:
        self.journal.log_event("final_reconciliation", {"status": "paper_noop"})
        return {"status": "paper_noop"}

    def _compute_fill_price(self, order_payload: dict, side: str) -> float | None:
        policy = (self.paper_cfg.get("fill_policy") or "mid").lower()
        bid = order_payload.get("bid")
        ask = order_payload.get("ask")
        mid = order_payload.get("mid")
        last = order_payload.get("last_price")
        limit = order_payload.get("limit_price")

        price = None
        if policy == "ask":
            price = ask or limit or mid
        elif policy == "bid":
            price = bid or limit or mid
        elif policy == "last":
            price = last or limit or mid
        else:
            price = mid or limit
            if price is None and bid is not None and ask is not None:
                price = (float(bid) + float(ask)) / 2
        if price is None:
            return None

        if bid is not None and ask is not None:
            spread = float(ask) - float(bid)
        else:
            spread = 0.0
        spread_factor = float(self.paper_cfg.get("slippage_spread_factor", 0.0) or 0.0)
        if spread_factor and spread > 0:
            if side == "BUY":
                price = float(price) + (spread * spread_factor)
            else:
                price = float(price) - (spread * spread_factor)

        slippage_bps = float(self.paper_cfg.get("slippage_bps", 0.0) or 0.0)
        if slippage_bps:
            adjust = 1 + (slippage_bps / 10000.0)
            if side == "SELL":
                adjust = 1 - (slippage_bps / 10000.0)
            price = float(price) * adjust
        return float(price)

    def _compute_fill_pnl(
        self,
        order_payload: dict,
        side: str,
        qty: int,
        fill_price: float,
    ) -> float | None:
        if side != "SELL":
            return None
        entry_price = order_payload.get("entry_price")
        if entry_price is None:
            return None
        try:
            entry_price = float(entry_price)
        except (TypeError, ValueError):
            return None
        contract_multiplier = (
            self.paper_cfg.get("contract_multiplier", 100) or 100
        )
        pnl = (float(fill_price) - entry_price) * qty * float(contract_multiplier)
        return round(pnl, 2)


class WebullExecutionAdapter(ExecutionAdapter):
    def __init__(self, config: dict, journal: EventJournal) -> None:
        self.config = config
        self.journal = journal
        self.exec_cfg = config.get("execution", {})
        self.webull_cfg = self.exec_cfg.get("webull", {})
        self.paper_fallback = PaperExecutionAdapter(config, journal)
        self._client: TradeClient | None = None

    def submit_order(self, order_payload: dict) -> dict:
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        if mode != "live":
            if self.webull_cfg.get("paper_only", False):
                self.journal.log_event(
                    "execution_warning",
                    {
                        "reason": "Webull paper_only enabled; using paper simulation.",
                        "adapter": "webull",
                        "mode": mode,
                    },
                )
                result = self.paper_fallback.submit_order(order_payload)
                result["adapter"] = "webull"
                result["mode"] = mode
                return result
            if not self._credentials_present(mode):
                if not self.webull_cfg.get("simulate_if_unavailable", True):
                    raise RuntimeError("Webull credentials missing for paper execution.")
                self.journal.log_event(
                    "execution_warning",
                    {
                        "reason": "Webull credentials missing; using paper simulation.",
                        "adapter": "webull",
                        "mode": mode,
                    },
                )
                result = self.paper_fallback.submit_order(order_payload)
                result["adapter"] = "webull"
                result["mode"] = mode
                return result

            if not self.webull_cfg.get("account_id"):
                if not self.webull_cfg.get("simulate_on_error", True):
                    raise RuntimeError("Missing execution.webull.account_id for paper execution.")
                self.journal.log_event(
                    "execution_warning",
                    {
                        "reason": "Missing execution.webull.account_id; using paper simulation.",
                        "adapter": "webull",
                        "mode": mode,
                    },
                )
                result = self.paper_fallback.submit_order(order_payload)
                result["adapter"] = "webull"
                result["mode"] = mode
                return result

            try:
                return self._submit_webull_option(order_payload, mode)
            except Exception as exc:
                if not self.webull_cfg.get("simulate_on_error", True):
                    raise
                self.journal.log_event(
                    "execution_warning",
                    {
                        "reason": f"Webull paper submit failed: {exc}",
                        "adapter": "webull",
                        "mode": mode,
                    },
                )
                result = self.paper_fallback.submit_order(order_payload)
                result["adapter"] = "webull"
                result["mode"] = mode
                return result

        raise RuntimeError("Live Webull execution is not enabled in this build.")

    def cancel_order(self, order_id: str) -> dict:
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        if mode != "live":
            return self.paper_fallback.cancel_order(order_id)
        raise RuntimeError("Live Webull execution is not enabled in this build.")

    def reconcile(self) -> dict:
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        if mode != "live":
            return self.paper_fallback.reconcile()
        raise RuntimeError("Live Webull execution is not enabled in this build.")

    def _credentials_present(self, mode: str) -> bool:
        if ApiClient is None or TradeClient is None:
            return False
        cfg = self.config.get("webull", {})
        app_key, app_secret = _get_webull_credentials(cfg, mode)
        return bool(app_key and app_secret)

    def _submit_webull_option(self, order_payload: dict, mode: str) -> dict:
        client = self._ensure_client()
        account_id = self.webull_cfg.get("account_id")
        asset_class = (order_payload.get("asset_class") or "OPTION").upper()
        if asset_class == "STOCK":
            order, client_order_id = self._build_stock_order(order_payload)
            self.journal.log_event(
                "submit",
                {
                    "order_id": client_order_id,
                    "status": "submitted",
                    "submission_time_utc": _utc_now_iso(),
                    "order_payload": order_payload,
                    "adapter": "webull",
                    "mode": mode,
                },
            )
            response = client.order_v2.place_order(account_id, [order])
            response_payload = _safe_json(response)
            status_code = getattr(response, "status_code", None)
            self.journal.log_event(
                "ack",
                {
                    "order_id": client_order_id,
                    "status": "acknowledged" if status_code == 200 else "rejected",
                    "ack_time_utc": _utc_now_iso(),
                    "status_code": status_code,
                    "response": response_payload,
                    "adapter": "webull",
                    "mode": mode,
                },
            )
            if status_code != 200:
                raise RuntimeError(f"Webull order rejected ({status_code})")

            return ExecutionResponse(
                status="submitted",
                order_id=_extract_order_id(response_payload) or client_order_id,
            ).to_dict()

        order, client_order_id = self._build_option_order(order_payload)
        use_preview = bool(self.webull_cfg.get("use_preview", False))

        self.journal.log_event(
            "submit",
            {
                "order_id": client_order_id,
                "status": "submitted",
                "submission_time_utc": _utc_now_iso(),
                "order_payload": order_payload,
                "adapter": "webull",
                "mode": mode,
            },
        )

        if use_preview:
            preview_res = client.order_v2.preview_option(account_id, [order])
            self.journal.log_event(
                "preview",
                {
                    "order_id": client_order_id,
                    "status_code": getattr(preview_res, "status_code", None),
                    "response": _safe_json(preview_res),
                    "adapter": "webull",
                    "mode": mode,
                },
            )
            if getattr(preview_res, "status_code", None) != 200:
                raise RuntimeError(f"Preview failed ({preview_res.status_code})")

        response = client.order_v2.place_option(account_id, [order])
        response_payload = _safe_json(response)
        status_code = getattr(response, "status_code", None)
        self.journal.log_event(
            "ack",
            {
                "order_id": client_order_id,
                "status": "acknowledged" if status_code == 200 else "rejected",
                "ack_time_utc": _utc_now_iso(),
                "status_code": status_code,
                "response": response_payload,
                "adapter": "webull",
                "mode": mode,
            },
        )
        if status_code != 200:
            raise RuntimeError(f"Webull order rejected ({status_code})")

        return ExecutionResponse(
            status="submitted",
            order_id=_extract_order_id(response_payload) or client_order_id,
        ).to_dict()

    def _ensure_client(self) -> TradeClient:
        if self._client is not None:
            return self._client
        if ApiClient is None or TradeClient is None:
            raise RuntimeError("Webull SDK not installed.")
        cfg = self.config.get("webull", {})
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        app_key, app_secret = _get_webull_credentials(cfg, mode)
        region = cfg.get("region", "us")
        if not app_key or not app_secret:
            raise RuntimeError("Missing Webull app_key/app_secret.")
        api_client = ApiClient(app_key, app_secret, region)
        api_endpoint = (
            self.webull_cfg.get("paper_endpoint")
            or self.webull_cfg.get("api_endpoint")
            or "https://us-openapi-alb.uat.webullbroker.com"
        )
        api_endpoint = api_endpoint.rstrip("/")
        api_client.add_endpoint(region, api_endpoint)
        self._client = TradeClient(api_client)
        return self._client

    def _build_option_order(self, order_payload: dict) -> tuple[dict, str]:
        client_order_id = uuid.uuid4().hex
        qty = int(order_payload.get("qty") or order_payload.get("contracts") or 0)
        if qty <= 0:
            raise RuntimeError("Order quantity must be greater than zero.")
        strike = order_payload.get("strike")
        exp_date = order_payload.get("expiration")
        option_type = (order_payload.get("option_type") or order_payload.get("direction") or "CALL").upper()
        symbol = order_payload.get("symbol")
        if not symbol or strike is None or exp_date is None:
            raise RuntimeError("Missing option contract details for order.")
        order_type = (order_payload.get("order_type") or "LIMIT").upper()
        limit_price = order_payload.get("limit_price")
        tif = (order_payload.get("tif") or "DAY").upper()
        if order_type == "LIMIT" and limit_price is None:
            raise RuntimeError("Limit price required for LIMIT orders.")

        order = {
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "order_type": order_type,
            "quantity": str(qty),
            "limit_price": f"{limit_price:.4f}" if limit_price is not None else None,
            "option_strategy": "SINGLE",
            "side": "BUY",
            "time_in_force": tif,
            "entrust_type": "QTY",
            "legs": [
                {
                    "side": "BUY",
                    "quantity": str(qty),
                    "symbol": symbol,
                    "strike_price": f"{strike}",
                    "init_exp_date": exp_date,
                    "instrument_type": "OPTION",
                    "option_type": option_type,
                    "market": "US",
                }
            ],
        }
        if order["limit_price"] is None:
            order.pop("limit_price")
        return order, client_order_id

    def _build_stock_order(self, order_payload: dict) -> tuple[dict, str]:
        client_order_id = uuid.uuid4().hex
        qty = int(order_payload.get("qty") or order_payload.get("contracts") or 0)
        if qty <= 0:
            raise RuntimeError("Order quantity must be greater than zero.")
        symbol = order_payload.get("symbol")
        if not symbol:
            raise RuntimeError("Missing stock symbol for order.")
        order_type = (order_payload.get("order_type") or "LIMIT").upper()
        limit_price = order_payload.get("limit_price")
        tif = (order_payload.get("tif") or "DAY").upper()
        side = (order_payload.get("side") or "BUY").upper()
        if order_type == "LIMIT" and limit_price is None:
            raise RuntimeError("Limit price required for LIMIT orders.")

        order = {
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "order_type": order_type,
            "quantity": str(qty),
            "limit_price": f"{limit_price:.4f}" if limit_price is not None else None,
            "side": side,
            "time_in_force": tif,
            "entrust_type": "QTY",
            "instrument_type": "EQUITY",
            "symbol": symbol,
            "market": "US",
            "support_trading_session": self.webull_cfg.get("support_trading_session", "CORE"),
        }
        if order["limit_price"] is None:
            order.pop("limit_price")
        return order, client_order_id


def _safe_json(response) -> dict:
    if response is None:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def _extract_order_id(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("order_id", "orderId", "client_order_id", "clientOrderId"):
        value = payload.get(key)
        if value:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("order_id", "orderId", "client_order_id", "clientOrderId"):
            value = data.get(key)
            if value:
                return str(value)
    return None


def _normalize_webull_mode(mode: str | None) -> str:
    resolved = str(mode or "paper").strip().lower()
    if resolved not in {"paper", "live"}:
        raise RuntimeError("Invalid Webull mode. Use 'paper' or 'live'.")
    return resolved


def _resolve_webull_endpoint(config: dict, mode: str) -> str:
    cfg = config.get("webull", {})
    exec_cfg = config.get("execution", {}).get("webull", {})
    if mode == "paper":
        endpoint = (
            exec_cfg.get("paper_endpoint")
            or exec_cfg.get("api_endpoint")
            or cfg.get("api_endpoint")
            or "https://us-openapi-alb.uat.webullbroker.com"
        )
    else:
        endpoint = exec_cfg.get("api_endpoint") or cfg.get("api_endpoint")
    if endpoint is None:
        return ""
    return str(endpoint).rstrip("/")


def _resolve_webull_account_id(
    config: dict,
    account_id: str | None = None,
    *,
    mode: str = "paper",
) -> str:
    exec_cfg = config.get("execution", {}).get("webull", {})
    env_specific = (
        "WEBULL_LIVE_ACCOUNT_ID"
        if str(mode).lower() == "live"
        else "WEBULL_PAPER_ACCOUNT_ID"
    )
    resolved_account_id = (
        account_id
        or exec_cfg.get("account_id")
        or os.environ.get(env_specific)
        or os.environ.get("WEBULL_ACCOUNT_ID")
    )
    if not resolved_account_id:
        raise RuntimeError(
            "Missing execution.webull.account_id "
            f"(set in config or env {env_specific} / WEBULL_ACCOUNT_ID)."
        )
    return str(resolved_account_id)


def _build_webull_trade_client(config: dict, mode: str) -> tuple[TradeClient, str]:
    if ApiClient is None or TradeClient is None:
        raise RuntimeError("Webull SDK not installed.")
    cfg = config.get("webull", {})
    app_key, app_secret = _get_webull_credentials(cfg, mode)
    if not app_key or not app_secret:
        if mode == "paper":
            raise RuntimeError("Missing Webull test_app_key/test_app_secret for paper.")
        raise RuntimeError("Missing Webull app_key/app_secret for live.")

    region = cfg.get("region", "us")
    api_client = ApiClient(app_key, app_secret, region)
    api_endpoint = _resolve_webull_endpoint(config, mode)
    if api_endpoint:
        api_client.add_endpoint(region, api_endpoint)
    trade_client = TradeClient(api_client)
    return trade_client, api_endpoint


def list_webull_paper_accounts(config: dict) -> dict:
    trade_client, api_endpoint = _build_webull_trade_client(config, "paper")
    response = trade_client.account_v2.get_account_list()
    payload = _safe_json(response)
    return {
        "status_code": getattr(response, "status_code", None),
        "endpoint": api_endpoint,
        "data": payload,
    }


def format_account_list(result: dict) -> str:
    status_code = result.get("status_code")
    payload = result.get("data") or {}
    lines = [f"Status: {status_code}"]
    accounts = None
    if isinstance(payload, dict):
        accounts = payload.get("data") or payload.get("account_list") or payload.get("accounts")
    if isinstance(accounts, list) and accounts:
        lines.append("Accounts:")
        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_id = account.get("account_id") or account.get("accountId")
            account_type = account.get("account_type") or account.get("accountType")
            status = account.get("status")
            lines.append(f"- {account_id} ({account_type}) {status or ''}".strip())
    else:
        lines.append(json.dumps(payload, indent=2))
    return "\n".join(lines)


def list_webull_account_balance(
    config: dict,
    account_id: str | None = None,
    mode: str = "paper",
) -> dict:
    mode = _normalize_webull_mode(mode)
    trade_client, api_endpoint = _build_webull_trade_client(config, mode)
    resolved_account_id = _resolve_webull_account_id(config, account_id)
    exec_cfg = config.get("execution", {}).get("webull", {})
    retries = int(exec_cfg.get("balance_retries", 2) or 0)
    last_exc = None
    for attempt in range(retries + 1):
        try:
            response = trade_client.account_v2.get_account_balance(resolved_account_id)
            payload = _safe_json(response)
            return {
                "status_code": getattr(response, "status_code", None),
                "endpoint": api_endpoint,
                "account_id": resolved_account_id,
                "mode": mode,
                "data": payload,
            }
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(1.5)
    raise last_exc


def format_balance_summary(result: dict) -> str:
    status_code = result.get("status_code")
    payload = result.get("data") or {}
    account_id = result.get("account_id")
    mode = result.get("mode", "paper")
    lines = [f"Status: {status_code}", f"Mode: {mode}", f"Account: {account_id}"]
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        keys = [
            "buying_power",
            "day_trading_buying_power",
            "cash_balance",
            "available_cash",
            "equity_value",
            "net_account_value",
            "maintenance_margin",
        ]
        extracted = {key: data.get(key) for key in keys if key in data}
        if extracted:
            lines.append("Summary:")
            for key, value in extracted.items():
                lines.append(f"- {key}: {value}")
    lines.append("Raw:")
    lines.append(json.dumps(payload, indent=2))
    return "\n".join(lines)


def list_webull_order_history(
    config: dict,
    account_id: str | None = None,
    mode: str = "paper",
) -> dict:
    mode = _normalize_webull_mode(mode)
    trade_client, api_endpoint = _build_webull_trade_client(config, mode)
    resolved_account_id = _resolve_webull_account_id(config, account_id)
    response = trade_client.order_v2.get_order_history(resolved_account_id, page_size=50)
    payload = _safe_json(response)
    open_order_ids = _extract_open_order_ids(payload)
    return {
        "status_code": getattr(response, "status_code", None),
        "endpoint": api_endpoint,
        "account_id": resolved_account_id,
        "mode": mode,
        "data": payload,
        "open_order_ids": open_order_ids,
    }


def fetch_webull_trade_history(
    config: dict,
    account_id: str | None = None,
    mode: str = "paper",
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = 100,
    max_pages: int = 200,
) -> dict:
    mode = _normalize_webull_mode(mode)
    trade_client, api_endpoint = _build_webull_trade_client(config, mode)
    resolved_account_id = _resolve_webull_account_id(config, account_id)

    if max_pages <= 0:
        raise RuntimeError("max_pages must be greater than zero.")
    if page_size <= 0:
        raise RuntimeError("page_size must be greater than zero.")
    normalized_page_size = max(1, min(int(page_size), 500))

    if end_date is None:
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if start_date is None:
        start_date = "2000-01-01"

    last_client_order_id: str | None = None
    last_order_id: str | None = None
    status_codes: list[int | None] = []
    pages_fetched = 0
    all_orders: list[dict] = []
    seen_order_keys: set[str] = set()
    seen_cursors: set[str] = set()

    while pages_fetched < max_pages:
        response = trade_client.order_v2.get_order_history(
            resolved_account_id,
            page_size=normalized_page_size,
            start_date=start_date,
            end_date=end_date,
            last_client_order_id=last_client_order_id,
            last_order_id=last_order_id,
        )
        payload = _safe_json(response)
        status_codes.append(getattr(response, "status_code", None))
        pages_fetched += 1

        page_orders = _extract_order_records(payload)
        for order in page_orders:
            key = _order_identity(order)
            if key in seen_order_keys:
                continue
            seen_order_keys.add(key)
            all_orders.append(order)

        next_client_order_id, next_order_id = _extract_history_cursor(payload)
        has_more = _extract_history_has_more(payload)
        if not page_orders:
            break
        if not (next_client_order_id or next_order_id):
            # Some payloads omit explicit cursor fields; use the last row cursor only
            # when the page suggests there may still be more records.
            if has_more is True or len(page_orders) >= normalized_page_size:
                last = page_orders[-1]
                if isinstance(last, dict):
                    next_client_order_id = str(
                        last.get("client_order_id") or last.get("clientOrderId") or ""
                    ) or None
                    next_order_id = str(last.get("order_id") or last.get("orderId") or "") or None
            if not (next_client_order_id or next_order_id):
                break
        if has_more is False and not (next_client_order_id or next_order_id):
            break
        cursor_key = f"{next_client_order_id or ''}|{next_order_id or ''}"
        if cursor_key in seen_cursors:
            break
        seen_cursors.add(cursor_key)
        if next_client_order_id == last_client_order_id and next_order_id == last_order_id:
            break
        last_client_order_id = next_client_order_id
        last_order_id = next_order_id

    return {
        "account_id": resolved_account_id,
        "mode": mode,
        "endpoint": api_endpoint,
        "query": {
            "start_date": start_date,
            "end_date": end_date,
            "page_size": normalized_page_size,
            "max_pages": max_pages,
        },
        "pages_fetched": pages_fetched,
        "status_codes": status_codes,
        "orders": all_orders,
        "order_count": len(all_orders),
        "open_order_ids": _extract_open_order_ids({"data": {"orders": all_orders}}),
    }


def analyze_webull_trade_history(history_result: dict) -> dict:
    orders = history_result.get("orders") or []

    filled_statuses = {"FILLED", "EXECUTED", "PARTIAL", "PARTIAL_FILLED", "PARTIALLY_FILLED"}
    cancelled_statuses = {"CANCELLED", "CANCELED", "EXPIRED", "CANCELING", "CANCELLING"}
    rejected_statuses = {"REJECTED", "FAILED"}

    first_trade_ts: datetime | None = None
    last_trade_ts: datetime | None = None
    status_counts: dict[str, int] = {}
    side_counts: dict[str, int] = {}
    by_symbol: dict[str, dict] = {}
    by_day: dict[str, dict] = {}

    filled_orders = 0
    cancelled_orders = 0
    rejected_orders = 0
    other_orders = 0
    buy_orders = 0
    sell_orders = 0
    gross_buy_notional = 0.0
    gross_sell_notional = 0.0
    realized_pnl = 0.0
    realized_pnl_count = 0

    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(
            order.get("status") or order.get("order_status") or order.get("orderStatus") or "UNKNOWN"
        ).strip().upper()
        side = str(
            order.get("side") or order.get("order_side") or order.get("orderSide") or "UNKNOWN"
        ).strip().upper()
        symbol = _extract_order_symbol(order)
        ts = _parse_trade_timestamp(
            order.get("filled_time")
            or order.get("filledTime")
            or order.get("update_time")
            or order.get("updateTime")
            or order.get("create_time")
            or order.get("createTime")
            or order.get("submitted_time")
            or order.get("submittedTime")
            or order.get("time")
            or order.get("timestamp")
        )

        status_counts[status] = status_counts.get(status, 0) + 1
        side_counts[side] = side_counts.get(side, 0) + 1

        if side.startswith("BUY"):
            buy_orders += 1
        elif side.startswith("SELL"):
            sell_orders += 1

        if status in filled_statuses:
            filled_orders += 1
        elif status in cancelled_statuses:
            cancelled_orders += 1
        elif status in rejected_statuses:
            rejected_orders += 1
        else:
            other_orders += 1

        if ts is not None:
            if first_trade_ts is None or ts < first_trade_ts:
                first_trade_ts = ts
            if last_trade_ts is None or ts > last_trade_ts:
                last_trade_ts = ts

        qty = _coerce_int(
            order.get("filled_quantity")
            or order.get("filledQuantity")
            or order.get("filled_qty")
            or order.get("filledQty")
            or order.get("executed_quantity")
            or order.get("executedQuantity")
            or order.get("exec_quantity")
            or order.get("execQty")
            or order.get("quantity")
            or order.get("qty")
        )
        px = _coerce_float(
            order.get("avg_fill_price")
            or order.get("average_fill_price")
            or order.get("avgFillPrice")
            or order.get("averageFillPrice")
            or order.get("filled_price")
            or order.get("filledPrice")
            or order.get("fill_price")
            or order.get("fillPrice")
            or order.get("limit_price")
            or order.get("limitPrice")
            or order.get("price")
        )
        order_realized_pnl = _coerce_float(
            order.get("realized_pnl")
            or order.get("realizedPnl")
            or order.get("pnl")
            or order.get("realized_profit")
            or order.get("realizedProfit")
        )
        if order_realized_pnl is not None:
            realized_pnl += order_realized_pnl
            realized_pnl_count += 1

        if symbol not in by_symbol:
            by_symbol[symbol] = {
                "symbol": symbol,
                "orders": 0,
                "filled_orders": 0,
                "buy_qty": 0,
                "sell_qty": 0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "net_qty": 0,
                "realized_pnl": 0.0,
            }
        symbol_row = by_symbol[symbol]
        symbol_row["orders"] += 1
        if status in filled_statuses:
            symbol_row["filled_orders"] += 1
        if order_realized_pnl is not None:
            symbol_row["realized_pnl"] += order_realized_pnl

        day_key = ts.date().isoformat() if ts is not None else "UNKNOWN"
        if day_key not in by_day:
            by_day[day_key] = {
                "date": day_key,
                "orders": 0,
                "filled_orders": 0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "realized_pnl": 0.0,
            }
        day_row = by_day[day_key]
        day_row["orders"] += 1
        if status in filled_statuses:
            day_row["filled_orders"] += 1
        if order_realized_pnl is not None:
            day_row["realized_pnl"] += order_realized_pnl

        if status in filled_statuses and qty is not None and px is not None:
            notional = float(qty) * float(px)
            if side.startswith("BUY"):
                gross_buy_notional += notional
                symbol_row["buy_qty"] += qty
                symbol_row["buy_notional"] += notional
                symbol_row["net_qty"] += qty
                day_row["buy_notional"] += notional
            elif side.startswith("SELL"):
                gross_sell_notional += notional
                symbol_row["sell_qty"] += qty
                symbol_row["sell_notional"] += notional
                symbol_row["net_qty"] -= qty
                day_row["sell_notional"] += notional

    by_symbol_rows = sorted(
        by_symbol.values(),
        key=lambda row: (row["orders"], row["symbol"]),
        reverse=True,
    )
    for row in by_symbol_rows:
        buy_qty = row["buy_qty"]
        sell_qty = row["sell_qty"]
        row["buy_notional"] = round(row["buy_notional"], 2)
        row["sell_notional"] = round(row["sell_notional"], 2)
        row["realized_pnl"] = round(row["realized_pnl"], 2)
        row["avg_buy_price"] = round(row["buy_notional"] / buy_qty, 4) if buy_qty > 0 else None
        row["avg_sell_price"] = round(row["sell_notional"] / sell_qty, 4) if sell_qty > 0 else None

    by_day_rows = sorted(by_day.values(), key=lambda row: row["date"])
    for row in by_day_rows:
        row["buy_notional"] = round(row["buy_notional"], 2)
        row["sell_notional"] = round(row["sell_notional"], 2)
        row["realized_pnl"] = round(row["realized_pnl"], 2)

    summary = {
        "orders_total": len(orders),
        "filled_orders": filled_orders,
        "cancelled_orders": cancelled_orders,
        "rejected_orders": rejected_orders,
        "other_orders": other_orders,
        "buy_orders": buy_orders,
        "sell_orders": sell_orders,
        "unique_symbols": len(by_symbol_rows),
        "gross_buy_notional": round(gross_buy_notional, 2),
        "gross_sell_notional": round(gross_sell_notional, 2),
        "net_cash_flow": round(gross_sell_notional - gross_buy_notional, 2),
        "first_trade_time_utc": first_trade_ts.isoformat() if first_trade_ts else None,
        "last_trade_time_utc": last_trade_ts.isoformat() if last_trade_ts else None,
        "realized_pnl": round(realized_pnl, 2) if realized_pnl_count > 0 else None,
        "realized_pnl_orders": realized_pnl_count,
    }
    return {
        "metadata": {
            "account_id": history_result.get("account_id"),
            "mode": history_result.get("mode"),
            "endpoint": history_result.get("endpoint"),
            "query": history_result.get("query") or {},
            "pages_fetched": history_result.get("pages_fetched"),
            "status_codes": history_result.get("status_codes") or [],
        },
        "summary": summary,
        "by_status": status_counts,
        "by_side": side_counts,
        "by_symbol": by_symbol_rows,
        "by_day": by_day_rows,
    }


def format_webull_trade_analysis(analysis: dict) -> str:
    metadata = analysis.get("metadata") or {}
    summary = analysis.get("summary") or {}
    by_status = analysis.get("by_status") or {}
    by_symbol = analysis.get("by_symbol") or []
    query = metadata.get("query") or {}

    lines = [
        f"Mode: {metadata.get('mode')}",
        f"Account: {metadata.get('account_id')}",
        f"Endpoint: {metadata.get('endpoint') or 'sdk_default'}",
        f"Date range: {query.get('start_date')} -> {query.get('end_date')}",
        f"Pages fetched: {metadata.get('pages_fetched')}",
        f"HTTP statuses: {metadata.get('status_codes')}",
        "",
        "Summary:",
        f"- Orders: {summary.get('orders_total', 0)}",
        f"- Filled: {summary.get('filled_orders', 0)}",
        f"- Cancelled: {summary.get('cancelled_orders', 0)}",
        f"- Rejected: {summary.get('rejected_orders', 0)}",
        f"- Symbols: {summary.get('unique_symbols', 0)}",
        f"- Gross buy notional: {summary.get('gross_buy_notional', 0.0)}",
        f"- Gross sell notional: {summary.get('gross_sell_notional', 0.0)}",
        f"- Net cash flow: {summary.get('net_cash_flow', 0.0)}",
        f"- Realized PnL (reported): {summary.get('realized_pnl')}",
        f"- First trade UTC: {summary.get('first_trade_time_utc')}",
        f"- Last trade UTC: {summary.get('last_trade_time_utc')}",
    ]
    if by_status:
        lines.append("")
        lines.append("By status:")
        for status, count in sorted(by_status.items(), key=lambda item: item[0]):
            lines.append(f"- {status}: {count}")

    if by_symbol:
        lines.append("")
        lines.append("Top symbols:")
        for row in by_symbol[:10]:
            lines.append(
                "- "
                f"{row.get('symbol')}: orders={row.get('orders')}, "
                f"filled={row.get('filled_orders')}, "
                f"buy_notional={row.get('buy_notional')}, "
                f"sell_notional={row.get('sell_notional')}, "
                f"net_qty={row.get('net_qty')}, "
                f"realized_pnl={row.get('realized_pnl')}"
            )
    return "\n".join(lines)


def cancel_webull_order(
    config: dict,
    client_order_id: str,
    account_id: str | None = None,
    mode: str = "paper",
) -> dict:
    mode = _normalize_webull_mode(mode)
    trade_client, api_endpoint = _build_webull_trade_client(config, mode)
    resolved_account_id = _resolve_webull_account_id(config, account_id)
    response = trade_client.order_v2.cancel_order(resolved_account_id, client_order_id)
    payload = _safe_json(response)
    return {
        "status_code": getattr(response, "status_code", None),
        "endpoint": api_endpoint,
        "account_id": resolved_account_id,
        "mode": mode,
        "client_order_id": client_order_id,
        "data": payload,
    }


def format_order_list(result: dict) -> str:
    status_code = result.get("status_code")
    payload = result.get("data") or {}
    account_id = result.get("account_id")
    mode = result.get("mode", "paper")
    lines = [f"Status: {status_code}", f"Mode: {mode}", f"Account: {account_id}"]
    open_ids = result.get("open_order_ids") or []
    if open_ids:
        lines.append("Open order IDs:")
        for order_id in open_ids:
            lines.append(f"- {order_id}")
    lines.append("Raw:")
    lines.append(json.dumps(payload, indent=2))
    return "\n".join(lines)


def _extract_open_order_ids(payload: dict) -> list[str]:
    open_ids: list[str] = []
    orders = _extract_order_records(payload)
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or order.get("order_status") or order.get("orderStatus") or "").upper()
        client_id = order.get("client_order_id") or order.get("clientOrderId")
        if status in {"NEW", "OPEN", "WORKING", "SUBMITTED", "PENDING"} and client_id:
            open_ids.append(str(client_id))
    return open_ids


def _extract_order_records(payload: dict | list | None) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
        return _dedupe_order_rows(records) if records else []
    if not isinstance(payload, dict):
        return []

    candidates: list[dict] = []
    queue: list[dict | list] = [payload]
    seen: set[int] = set()
    while queue:
        node = queue.pop(0)
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        if isinstance(node, list):
            dict_rows = [item for item in node if isinstance(item, dict)]
            if dict_rows:
                candidates.extend(dict_rows)
            for item in node:
                if isinstance(item, (dict, list)):
                    queue.append(item)
            continue
        for key, value in node.items():
            if isinstance(value, list):
                if key in {"orders", "order_list", "orderList", "items", "data", "rows", "list"}:
                    candidates.extend([item for item in value if isinstance(item, dict)])
                queue.append(value)
            elif isinstance(value, dict):
                queue.append(value)

    filtered = [item for item in candidates if _looks_like_order_record(item)]
    if filtered:
        return _dedupe_order_rows(filtered)

    data = payload.get("data")
    if isinstance(data, list):
        return _dedupe_order_rows([item for item in data if isinstance(item, dict)])
    return []


def _looks_like_order_record(record: dict) -> bool:
    keys = {
        "order_id",
        "orderId",
        "client_order_id",
        "clientOrderId",
        "symbol",
        "ticker",
        "side",
        "order_type",
        "orderType",
        "status",
        "order_status",
        "orderStatus",
    }
    return any(key in record for key in keys)


def _dedupe_order_rows(records: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for record in records:
        key = _order_identity(record)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _extract_history_cursor(payload: dict) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data")
    last_client_order_id = None
    last_order_id = None

    for node in (payload, data):
        if not isinstance(node, dict):
            continue
        for key in (
            "last_client_order_id",
            "lastClientOrderId",
            "next_last_client_order_id",
            "nextLastClientOrderId",
        ):
            value = node.get(key)
            if value:
                last_client_order_id = str(value)
                break
        for key in ("last_order_id", "lastOrderId", "next_last_order_id", "nextLastOrderId"):
            value = node.get(key)
            if value:
                last_order_id = str(value)
                break
    return last_client_order_id, last_order_id


def _extract_history_has_more(payload: dict) -> bool | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    for node in (payload, data):
        if not isinstance(node, dict):
            continue
        for key in ("has_more", "hasMore", "more", "is_more"):
            if key in node:
                return bool(node.get(key))
        for key in ("is_last_page", "isLastPage"):
            if key in node:
                return not bool(node.get(key))
    return None


def _order_identity(order: dict) -> str:
    fields = [
        order.get("order_id") or order.get("orderId"),
        order.get("client_order_id") or order.get("clientOrderId"),
        order.get("symbol") or order.get("ticker"),
        order.get("side") or order.get("order_side") or order.get("orderSide"),
        order.get("quantity") or order.get("qty"),
        order.get("create_time") or order.get("createTime") or order.get("update_time") or order.get("updateTime"),
    ]
    compact = "|".join(str(item) for item in fields if item is not None and item != "")
    if compact:
        return compact
    return json.dumps(order, sort_keys=True)


def _extract_order_symbol(order: dict) -> str:
    for key in ("symbol", "ticker", "stock", "instrument_symbol", "instrumentSymbol"):
        value = order.get(key)
        if value:
            return str(value).upper()
    instrument = order.get("instrument")
    if isinstance(instrument, dict):
        for key in ("symbol", "ticker", "name"):
            value = instrument.get(key)
            if value:
                return str(value).upper()
    legs = order.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            value = leg.get("symbol")
            if value:
                return str(value).upper()
    return "UNKNOWN"


def _parse_trade_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_trade_timestamp(int(text))
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _get_webull_credentials(cfg: dict, mode: str) -> tuple[str | None, str | None]:
    if mode != "live":
        app_key = cfg.get("test_app_key") or os.getenv("WEBULL_TEST_APP_KEY")
        app_secret = cfg.get("test_app_secret") or os.getenv("WEBULL_TEST_APP_SECRET")
        if app_key and app_secret:
            return app_key, app_secret
    app_key = cfg.get("app_key") or os.getenv("WEBULL_APP_KEY")
    app_secret = cfg.get("app_secret") or os.getenv("WEBULL_APP_SECRET")
    return app_key, app_secret


class TradierExecutionAdapter(ExecutionAdapter):
    def __init__(self, config: dict, journal: EventJournal) -> None:
        self.config = config
        self.journal = journal
        self.exec_cfg = config.get("execution", {})
        self.tradier_cfg = self.exec_cfg.get("tradier", {})
        self.paper_fallback = PaperExecutionAdapter(config, journal)

    def submit_order(self, order_payload: dict) -> dict:
        asset_class = (order_payload.get("asset_class") or "OPTION").upper()
        if asset_class != "OPTION":
            raise RuntimeError("Tradier adapter currently supports options only.")

        account_id = self._account_id()
        base_url = self._base_url()
        token = self._token()
        mode = (self.exec_cfg.get("mode") or "paper").lower()
        side = (order_payload.get("side") or "BUY").upper()
        qty = int(order_payload.get("qty") or order_payload.get("contracts") or 0)
        if qty <= 0:
            raise RuntimeError("Order quantity must be greater than zero.")

        option_symbol = (
            order_payload.get("option_symbol")
            or order_payload.get("symbol_full")
            or self._build_occ_symbol(
                order_payload.get("symbol"),
                order_payload.get("expiration"),
                order_payload.get("option_type") or order_payload.get("direction"),
                order_payload.get("strike"),
            )
        )
        if not option_symbol:
            raise RuntimeError("Missing option symbol for Tradier order.")

        order_type = (order_payload.get("order_type") or "LIMIT").lower()
        duration = (order_payload.get("tif") or "DAY").lower()
        if duration not in {"day", "gtc"}:
            duration = "day"
        limit_price = order_payload.get("limit_price")
        if order_type == "limit" and limit_price is None:
            raise RuntimeError("Limit price required for LIMIT orders.")

        side_value = "buy_to_open" if side == "BUY" else "sell_to_close"
        data = {
            "class": "option",
            "symbol": order_payload.get("symbol"),
            "option_symbol": option_symbol,
            "side": side_value,
            "quantity": qty,
            "type": order_type,
            "duration": duration,
        }
        if order_type == "limit":
            data["price"] = f"{float(limit_price):.2f}"
        if self.tradier_cfg.get("preview", False):
            data["preview"] = "true"

        self.journal.log_event(
            "submit",
            {
                "status": "submitted",
                "submission_time_utc": _utc_now_iso(),
                "order_payload": order_payload,
                "adapter": "tradier",
                "mode": mode,
            },
        )

        try:
            response = requests.post(
                f"{base_url}/accounts/{account_id}/orders",
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
            payload = {}
            try:
                payload = response.json()
            except Exception:
                payload = {"raw": response.text}
        except Exception as exc:
            if not self._simulate_on_error():
                raise
                self.journal.log_event(
                    "execution_warning",
                    {
                        "reason": f"Tradier submit failed: {exc}",
                        "adapter": "tradier",
                        "mode": mode,
                    },
                )
            result = self.paper_fallback.submit_order(order_payload)
            result["adapter"] = "tradier"
            result["mode"] = (self.exec_cfg.get("mode") or "paper").lower()
            return result

        if response.status_code >= 400:
            self.journal.log_event(
                "rejected",
                {
                    "status": "rejected",
                    "reason": "tradier_error",
                    "status_code": response.status_code,
                    "response": payload,
                    "adapter": "tradier",
                    "mode": mode,
                },
            )
            if self._simulate_on_error():
                self.journal.log_event(
                    "execution_warning",
                    {
                        "reason": f"Tradier rejected order; simulating fill. {payload}",
                        "adapter": "tradier",
                        "mode": mode,
                    },
                )
                result = self.paper_fallback.submit_order(order_payload)
                result["adapter"] = "tradier"
                result["mode"] = mode
                return result
            return ExecutionResponse(
                status="rejected",
                reason=str(payload),
            ).to_dict()

        order_info = payload.get("order") or {}
        order_id = order_info.get("id")
        status = order_info.get("status") or "unknown"
        errors = order_info.get("errors")
        if errors:
            status = "rejected"

        self.journal.log_event(
            "ack",
            {
                "order_id": order_id,
                "status": "acknowledged" if not errors else "rejected",
                "ack_time_utc": _utc_now_iso(),
                "response": payload,
                "adapter": "tradier",
                "mode": mode,
            },
        )

        if errors:
            return ExecutionResponse(
                status="rejected",
                order_id=str(order_id) if order_id else None,
                reason=str(errors),
            ).to_dict()

        fill_response = self._maybe_log_fill(order_id, order_payload, mode)
        if fill_response is not None:
            return fill_response

        return ExecutionResponse(
            status="submitted",
            order_id=str(order_id) if order_id else None,
            reason=None,
        ).to_dict()

    def cancel_order(self, order_id: str) -> dict:
        account_id = self._account_id()
        base_url = self._base_url()
        token = self._token()
        response = requests.delete(
            f"{base_url}/accounts/{account_id}/orders/{order_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        self.journal.log_event(
            "cancel_confirm",
            {
                "order_id": order_id,
                "status": "cancelled",
                "adapter": "tradier",
                "mode": (self.exec_cfg.get("mode") or "paper").lower(),
                "response": payload,
            },
        )
        return ExecutionResponse(status="cancelled", order_id=order_id).to_dict()

    def reconcile(self) -> dict:
        self.journal.log_event("final_reconciliation", {"status": "tradier_noop"})
        return {"status": "tradier_noop"}

    def get_positions(self) -> dict[str, int]:
        account_id = self._account_id()
        base_url = self._base_url()
        token = self._token()
        try:
            response = requests.get(
                f"{base_url}/accounts/{account_id}/positions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=15,
            )
            if response.status_code >= 400:
                return {}
            payload = response.json()
        except Exception:
            return {}

        positions_payload = payload.get("positions") or payload.get("position") or {}
        if isinstance(positions_payload, dict):
            positions_payload = (
                positions_payload.get("position")
                or positions_payload.get("positions")
                or positions_payload.get("data")
                or []
            )
        if isinstance(positions_payload, dict):
            positions = [positions_payload]
        elif isinstance(positions_payload, list):
            positions = positions_payload
        else:
            positions = []

        results: dict[str, int] = {}
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            option_symbol = pos.get("option_symbol") or pos.get("optionSymbol")
            if not option_symbol:
                continue
            qty = self._safe_int(pos.get("quantity") or pos.get("qty") or pos.get("position"))
            if qty is None:
                continue
            if qty <= 0:
                continue
            results[str(option_symbol)] = int(qty)
        return results

    def _account_id(self) -> str:
        account_id = self.tradier_cfg.get("account_id") or os.getenv("TRADIER_ACCOUNT_ID")
        if not account_id:
            raise RuntimeError("Missing execution.tradier.account_id or TRADIER_ACCOUNT_ID.")
        return str(account_id)

    def _token(self) -> str:
        sandbox = bool(self.tradier_cfg.get("sandbox", False))
        if sandbox:
            token = (
                self.tradier_cfg.get("sandbox_token")
                or self.config.get("tradier", {}).get("sandbox_token")
                or os.getenv("TRADIER_SANDBOX_TOKEN")
            )
        else:
            token = (
                self.tradier_cfg.get("access_token")
                or self.config.get("tradier", {}).get("access_token")
                or os.getenv("TRADIER_ACCESS_TOKEN")
            )
        if not token:
            raise RuntimeError("Missing Tradier access token for execution.")
        return token

    def _base_url(self) -> str:
        sandbox = bool(self.tradier_cfg.get("sandbox", False))
        if sandbox:
            return (
                self.tradier_cfg.get("sandbox_base_url")
                or self.config.get("tradier", {}).get("sandbox_base_url")
                or "https://sandbox.tradier.com/v1"
            ).rstrip("/")
        return (
            self.tradier_cfg.get("base_url")
            or self.config.get("tradier", {}).get("base_url")
            or "https://api.tradier.com/v1"
        ).rstrip("/")

    def _simulate_on_error(self) -> bool:
        return bool(self.tradier_cfg.get("simulate_on_error", True))

    def _simulate_fill_on_ack(self) -> bool:
        return bool(self.tradier_cfg.get("simulate_fill_on_ack", False))

    def _poll_fill_seconds(self) -> float:
        return float(self.tradier_cfg.get("fill_poll_seconds", 0) or 0.0)

    def _poll_fill_interval(self) -> float:
        return float(self.tradier_cfg.get("fill_poll_interval", 1.0) or 1.0)

    def _maybe_log_fill(self, order_id: str | None, order_payload: dict, mode: str) -> dict | None:
        if order_id:
            poll_result = self._poll_for_fill(str(order_id))
            if poll_result is not None:
                status = poll_result.get("status")
                if status in {"rejected", "canceled", "cancelled", "expired"}:
                    self.journal.log_event(
                        "rejected",
                        {
                            "order_id": str(order_id),
                            "status": "rejected",
                            "reason": f"tradier_status_{status}",
                            "response": poll_result.get("payload"),
                            "adapter": "tradier",
                            "mode": mode,
                        },
                    )
                    return ExecutionResponse(
                        status="rejected",
                        order_id=str(order_id),
                        reason=f"tradier_status_{status}",
                    ).to_dict()
                filled_qty = poll_result.get("filled_qty")
                fill_price = poll_result.get("fill_price")
                if filled_qty and fill_price is not None:
                    self._log_fill(
                        order_id=str(order_id),
                        order_payload=order_payload,
                        filled_qty=int(filled_qty),
                        fill_price=float(fill_price),
                        fill_source="tradier",
                        mode=mode,
                    )
                    return ExecutionResponse(
                        status="filled",
                        order_id=str(order_id),
                        filled_qty=int(filled_qty),
                        fill_price=round(float(fill_price), 4),
                    ).to_dict()

        if not self._simulate_fill_on_ack():
            return None
        simulated = self._simulate_fill(order_payload, order_id)
        if simulated is None:
            return None
        self._log_fill(
            order_id=str(order_id) if order_id else None,
            order_payload=order_payload,
            filled_qty=simulated["filled_qty"],
            fill_price=simulated["fill_price"],
            fill_source="simulated",
            mode=mode,
        )
        return ExecutionResponse(
            status="filled",
            order_id=str(order_id) if order_id else None,
            filled_qty=simulated["filled_qty"],
            fill_price=round(float(simulated["fill_price"]), 4),
            reason="simulated_fill_on_ack",
        ).to_dict()

    def _poll_for_fill(self, order_id: str) -> dict | None:
        max_wait = self._poll_fill_seconds()
        if max_wait <= 0:
            return None
        interval = max(0.5, self._poll_fill_interval())
        deadline = time.time() + max_wait
        while time.time() <= deadline:
            payload = self._fetch_order(order_id)
            if payload is None:
                break
            order = self._extract_order_payload(payload)
            if not order:
                break
            status = self._normalize_status(order.get("status"))
            filled_qty, fill_price = self._extract_fill_info(order, status)
            if status in {"filled", "partial", "partially_filled", "executed"} and filled_qty:
                return {
                    "status": status,
                    "filled_qty": filled_qty,
                    "fill_price": fill_price,
                    "payload": payload,
                }
            if status in {"rejected", "canceled", "cancelled", "expired"}:
                return {"status": status, "payload": payload}
            time.sleep(interval)
        return None

    def _fetch_order(self, order_id: str) -> dict | None:
        account_id = self._account_id()
        base_url = self._base_url()
        token = self._token()
        try:
            response = requests.get(
                f"{base_url}/accounts/{account_id}/orders/{order_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def _extract_order_payload(self, payload: dict | None) -> dict | None:
        if not isinstance(payload, dict):
            return None
        order = payload.get("order")
        if isinstance(order, dict):
            return order
        if isinstance(order, list) and order:
            if isinstance(order[0], dict):
                return order[0]
        data = payload.get("orders") or payload.get("data")
        if isinstance(data, dict) and "order" in data and isinstance(data["order"], dict):
            return data["order"]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def _normalize_status(self, status: object) -> str:
        if status is None:
            return "unknown"
        return str(status).strip().lower()

    def _extract_fill_info(self, order: dict, status: str) -> tuple[int | None, float | None]:
        filled_qty = self._safe_int(
            order.get("exec_quantity")
            or order.get("filled_quantity")
            or order.get("filled")
            or order.get("exec_qty")
        )
        fill_price = self._safe_float(
            order.get("avg_fill_price")
            or order.get("average_fill_price")
            or order.get("fill_price")
            or order.get("last_fill_price")
        )
        if filled_qty is None and status in {"filled", "partial", "partially_filled", "executed"}:
            filled_qty = self._safe_int(order.get("quantity"))
        if fill_price is None and status in {"filled", "partial", "partially_filled", "executed"}:
            fill_price = self._safe_float(order.get("price"))
        return filled_qty, fill_price

    def _simulate_fill(self, order_payload: dict, order_id: str | None) -> dict | None:
        side = (order_payload.get("side") or "BUY").upper()
        qty = int(order_payload.get("qty") or order_payload.get("contracts") or 0)
        if qty <= 0:
            return None
        fill_price = self.paper_fallback._compute_fill_price(order_payload, side)
        if fill_price is None:
            return None
        return {
            "order_id": str(order_id) if order_id else None,
            "filled_qty": qty,
            "fill_price": float(fill_price),
        }

    def _log_fill(
        self,
        order_id: str | None,
        order_payload: dict,
        filled_qty: int,
        fill_price: float,
        fill_source: str,
        mode: str,
    ) -> None:
        fill_pnl = self._compute_fill_pnl(order_payload, filled_qty, fill_price)
        self.journal.log_event(
            "fill",
            {
                "order_id": order_id,
                "status": "filled",
                "filled_qty": filled_qty,
                "fill_price": round(float(fill_price), 4),
                "fill_time_utc": _utc_now_iso(),
                "fill_pnl": fill_pnl,
                "order_payload": order_payload,
                "fill_source": fill_source,
                "adapter": "tradier",
                "mode": mode,
            },
        )

    def _compute_fill_pnl(self, order_payload: dict, qty: int, fill_price: float) -> float | None:
        side = (order_payload.get("side") or "BUY").upper()
        if side != "SELL":
            return None
        entry_price = order_payload.get("entry_price")
        if entry_price is None:
            return None
        try:
            entry_price = float(entry_price)
        except (TypeError, ValueError):
            return None
        contract_multiplier = (
            self.exec_cfg.get("paper", {}).get("contract_multiplier", 100) or 100
        )
        pnl = (float(fill_price) - entry_price) * qty * float(contract_multiplier)
        return round(pnl, 2)

    def _safe_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _build_occ_symbol(
        self,
        symbol: str | None,
        expiration: str | None,
        option_type: str | None,
        strike: float | None,
    ) -> str | None:
        if not symbol or not expiration or strike is None:
            return None
        try:
            exp = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
        except ValueError:
            return None
        opt = (option_type or "CALL").upper()
        opt = "C" if opt.startswith("C") else "P"
        strike_int = int(round(float(strike) * 1000))
        root = symbol.strip().upper()
        return f"{root}{exp}{opt}{strike_int:08d}"
