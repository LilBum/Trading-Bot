from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .engine import PlannerApp
from .models import PlanResult
from .positions import build_ledger_from_events


class PlanCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.plans: List[PlanResult] = []
        self.updated_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.session_totals: dict = {}
        self.session_accepts: list[dict] = []
        self.performance: dict = {}

    def update(
        self,
        plans: List[PlanResult],
        error: Optional[str] = None,
        session_totals: Optional[dict] = None,
        session_accepts: Optional[list[dict]] = None,
        performance: Optional[dict] = None,
    ) -> None:
        with self._lock:
            self.plans = plans
            self.updated_at = datetime.now(timezone.utc).isoformat()
            self.last_error = error
            if session_totals is not None:
                self.session_totals = session_totals
            if session_accepts is not None:
                self.session_accepts = session_accepts
            if performance is not None:
                self.performance = performance

    def snapshot(self) -> Tuple[List[PlanResult], Optional[str], Optional[str], dict, list[dict], dict]:
        with self._lock:
            return (
                list(self.plans),
                self.updated_at,
                self.last_error,
                dict(self.session_totals),
                list(self.session_accepts),
                dict(self.performance),
            )


def _update_loop(app: PlannerApp, cache: PlanCache, interval: int, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            plans, errors = app.run(log_to_journal=True)
            error_text = "; ".join(errors) if errors else None
            event_log_path = Path(app.config.get("logging", {}).get("event_log_path", "events.jsonl"))
            session_totals, session_accepts = _compute_session_stats(event_log_path)
            performance = _compute_performance_stats(
                event_log_path,
                app.provider,
                app.config,
                session_date_exchange=session_totals.get("session_date_exchange"),
            )
            cache.update(
                plans,
                error=error_text,
                session_totals=session_totals,
                session_accepts=session_accepts,
                performance=performance,
            )
        except Exception as exc:
            cache.update([], error=str(exc))
        stop_event.wait(interval)


def _make_handler(web_dir: Path, cache: PlanCache):
    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_dir), **kwargs)

        def do_GET(self) -> None:
            if self.path.startswith("/api/plans"):
                plans, updated_at, error, session_totals, session_accepts, performance = cache.snapshot()
                payload = {
                    "updated_at": updated_at,
                    "error": error,
                    "plans": [plan.to_dict() for plan in plans],
                    "session_totals": session_totals,
                    "session_accepts": session_accepts,
                    "performance": performance,
                }
                body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, format: str, *args) -> None:
            return

    return DashboardHandler


def start_dashboard(app: PlannerApp, host: str, port: int, interval: int) -> None:
    web_dir = Path(__file__).resolve().parents[1] / "web"
    if not web_dir.exists():
        raise FileNotFoundError(f"Web directory not found: {web_dir}")

    cache = PlanCache()
    stop_event = threading.Event()
    updater = threading.Thread(
        target=_update_loop,
        args=(app, cache, interval, stop_event),
        daemon=True,
    )
    updater.start()

    handler = _make_handler(web_dir, cache)
    server = ThreadingHTTPServer((host, port), handler)

    try:
        print(f"Web dashboard running at http://{host}:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping web dashboard.")
    finally:
        stop_event.set()
        server.shutdown()


def _compute_session_stats(
    event_log_path: Path,
    session_date_exchange: Optional[str] = None,
) -> tuple[dict, list[dict]]:
    if session_date_exchange is None:
        eastern = ZoneInfo("America/New_York")
        session_date_exchange = datetime.now(eastern).date().isoformat()

    totals = {
        "session_date_exchange": session_date_exchange,
        "total": 0,
        "allowed": 0,
        "rejected": 0,
        "avg_data_health": None,
        "stale_count": 0,
        "stale_rate": None,
    }
    accepts: dict[str, dict] = {}
    data_scores: list[float] = []

    if not event_log_path.exists():
        return totals, []

    try:
        lines = event_log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return totals, []

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event_type") != "plan":
            continue
        record_session = record.get("session_date_exchange")
        payload = record.get("payload", {})
        if not record_session:
            record_session = payload.get("session_date_exchange")
        if record_session and record_session != session_date_exchange:
            continue
        status = payload.get("status") or record.get("status")
        totals["total"] += 1
        score = payload.get("data_health_score")
        if isinstance(score, (int, float)):
            data_scores.append(float(score))
        reasons = payload.get("reject_reasons") or []
        if any("Stale market data" in reason for reason in reasons):
            totals["stale_count"] += 1
        if status == "ALLOWED":
            totals["allowed"] += 1
            symbol = payload.get("symbol") or record.get("symbol") or "UNKNOWN"
            option = payload.get("option_contract") or {}
            key = (
                f"{symbol}|{option.get('expiration')}|{option.get('strike')}|"
                f"{option.get('option_type')}"
            )
            accepts[key] = payload
        elif status == "REJECTED":
            totals["rejected"] += 1
    if data_scores:
        totals["avg_data_health"] = round(sum(data_scores) / len(data_scores), 3)
    if totals["total"] > 0:
        totals["stale_rate"] = round((totals["stale_count"] / totals["total"]) * 100.0, 2)
    return totals, list(accepts.values())


def _compute_performance_stats(
    event_log_path: Path,
    provider,
    config: dict,
    session_date_exchange: Optional[str] = None,
) -> dict:
    if session_date_exchange is None:
        eastern = ZoneInfo("America/New_York")
        session_date_exchange = datetime.now(eastern).date().isoformat()

    performance = {
        "session_date_exchange": session_date_exchange,
        "mode": config.get("execution", {}).get("mode", "paper"),
        "open_positions": [],
        "last_fills": [],
        "last_closed": [],
        "totals": {
            "open_positions": 0,
            "invested": 0.0,
            "market_value": 0.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": None,
            "max_drawdown": 0.0,
            "avg_slippage": None,
        },
    }

    if not event_log_path.exists():
        return performance

    try:
        lines = event_log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return performance

    fills: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event_type") != "fill":
            continue
        record_session = record.get("session_date_exchange")
        if record_session and record_session != session_date_exchange:
            continue
        payload = record.get("payload", {})
        fills.append(payload)

    if not fills:
        return performance

    fills_sorted = sorted(
        fills,
        key=lambda item: item.get("fill_time_utc") or "",
        reverse=True,
    )
    performance["last_fills"] = fills_sorted[:5]
    slippages = [
        float(item.get("fill_slippage"))
        for item in fills
        if isinstance(item.get("fill_slippage"), (int, float))
    ]
    if slippages:
        performance["totals"]["avg_slippage"] = round(sum(slippages) / len(slippages), 4)

    contract_multiplier = (
        config.get("execution", {}).get("paper", {}).get("contract_multiplier", 100) or 100
    )
    ledger = build_ledger_from_events(
        event_log_path,
        session_date_exchange=session_date_exchange,
        contract_multiplier=contract_multiplier,
    )
    if ledger.closed_trades:
        performance["last_closed"] = ledger.closed_trades[-5:]
    performance["totals"]["realized_pnl"] = round(ledger.realized_pnl, 2)
    performance["totals"]["trades"] = len(ledger.closed_trades)
    wins = len([t for t in ledger.closed_trades if t.realized_pnl > 0])
    if ledger.closed_trades:
        performance["totals"]["win_rate"] = round((wins / len(ledger.closed_trades)) * 100.0, 2)
        gross_win = sum(t.realized_pnl for t in ledger.closed_trades if t.realized_pnl > 0)
        gross_loss = sum(-t.realized_pnl for t in ledger.closed_trades if t.realized_pnl < 0)
        if gross_loss > 0:
            performance["totals"]["profit_factor"] = round(gross_win / gross_loss, 3)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        sorted_trades = sorted(
            ledger.closed_trades,
            key=lambda t: t.exit_time_utc or "",
        )
        for trade in sorted_trades:
            equity += trade.realized_pnl
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            if drawdown > max_dd:
                max_dd = drawdown
        performance["totals"]["max_drawdown"] = round(max_dd, 2)

    positions = ledger.positions
    if not positions:
        return performance

    chain_cache: dict[str, dict] = {}

    for key, pos in positions.items():
        symbol = pos.symbol
        expiration = pos.expiration
        strike = pos.strike
        option_type = pos.option_type
        if symbol not in chain_cache:
            try:
                chain_cache[symbol] = {
                    "data": provider.get_options_chain(symbol, config.get("options", {}).get("target_dte", 1))
                }
            except Exception:
                chain_cache[symbol] = {"data": None}
        chain_data = chain_cache[symbol]["data"]
        current_mid = None
        if chain_data is not None:
            _, chain = chain_data
            subset = chain[
                (chain["expiration"] == expiration)
                & (chain["strike"] == strike)
                & (chain["option_type"] == option_type)
            ]
            if not subset.empty:
                row = subset.iloc[0]
                bid = row.get("bid")
                ask = row.get("ask")
                last_price = row.get("last_price")
                if bid is not None and ask is not None and bid > 0 and ask > 0:
                    current_mid = (float(bid) + float(ask)) / 2.0
                elif last_price is not None and last_price > 0:
                    current_mid = float(last_price)

        qty = pos.qty
        avg_price = pos.avg_price
        invested = avg_price * qty * contract_multiplier
        market_value = current_mid * qty * contract_multiplier if current_mid is not None else None
        unrealized = market_value - invested if market_value is not None else None

        performance["open_positions"].append(
            {
                "symbol": symbol,
                "expiration": expiration,
                "strike": strike,
                "option_type": option_type,
                "qty": qty,
                "avg_price": round(avg_price, 4),
                "current_mid": round(current_mid, 4) if current_mid is not None else None,
                "invested": round(invested, 2),
                "market_value": round(market_value, 2) if market_value is not None else None,
                "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
                "entry_time_utc": pos.entry_time_utc,
            }
        )

    totals = performance["totals"]
    totals["open_positions"] = len(performance["open_positions"])
    totals["invested"] = round(sum(item["invested"] for item in performance["open_positions"]), 2)
    totals["market_value"] = round(
        sum(item["market_value"] or 0.0 for item in performance["open_positions"]), 2
    )
    totals["unrealized_pnl"] = round(
        sum(item["unrealized_pnl"] or 0.0 for item in performance["open_positions"]), 2
    )

    return performance
