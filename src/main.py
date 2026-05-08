from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import time
from pathlib import Path

from .config import load_config
from .engine import PlannerApp, print_plans
from .execution_adapter import format_account_list, list_webull_paper_accounts
from .journal import EventJournal
from .web_server import start_dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description="1DTE options trading planner")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously and emit a plan every interval",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between runs when --watch is enabled (default: from config or 300)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Run the local web dashboard",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for the web dashboard (default: from config or 8000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host for the web dashboard (default: from config or 127.0.0.1)",
    )
    parser.add_argument(
        "--record-pnl",
        type=float,
        default=None,
        help="Record a realized PnL value (adds an event and updates daily loss tracking)",
    )
    parser.add_argument(
        "--list-paper-accounts",
        action="store_true",
        help="List Webull paper account IDs from the UAT endpoint",
    )
    parser.add_argument(
        "--show-buying-power",
        action="store_true",
        help="Show Webull paper account buying power and balances",
    )
    parser.add_argument(
        "--account-id",
        type=str,
        default=None,
        help="Override account ID for paper trading queries",
    )
    parser.add_argument(
        "--webull-mode",
        type=str,
        default=None,
        help="Webull API mode for account/order utilities: paper or live",
    )
    parser.add_argument(
        "--list-open-orders",
        action="store_true",
        help="List recent Webull paper orders (best-effort for open orders)",
    )
    parser.add_argument(
        "--cancel-order",
        type=str,
        default=None,
        help="Cancel a Webull order by client order ID",
    )
    parser.add_argument(
        "--cancel-all-open",
        action="store_true",
        help="Cancel all open Webull orders returned by the list command",
    )
    parser.add_argument(
        "--submit-test-order",
        action="store_true",
        help="Submit a manual test order using execution settings",
    )
    parser.add_argument(
        "--analyze-webull-trades",
        action="store_true",
        help="Fetch paginated Webull trade history and print analytics",
    )
    parser.add_argument(
        "--trade-start-date",
        type=str,
        default=None,
        help="Trade history start date (YYYY-MM-DD). Default: 2000-01-01",
    )
    parser.add_argument(
        "--trade-end-date",
        type=str,
        default=None,
        help="Trade history end date (YYYY-MM-DD). Default: today UTC",
    )
    parser.add_argument(
        "--trade-page-size",
        type=int,
        default=100,
        help="Rows per Webull history page (default: 100)",
    )
    parser.add_argument(
        "--trade-max-pages",
        type=int,
        default=200,
        help="Max pages to fetch from Webull history (default: 200)",
    )
    parser.add_argument(
        "--trade-report-path",
        type=str,
        default=None,
        help="Optional path to write full JSON report (history + analysis)",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Replay a CSV of intraday bars for backtest-style analysis",
    )
    parser.add_argument("--replay-bars", type=str, default=None, help="Path to CSV of intraday bars")
    parser.add_argument("--replay-chain", type=str, default=None, help="Path to CSV of option chain snapshot")
    parser.add_argument("--replay-symbol", type=str, default=None, help="Symbol for replay run")
    parser.add_argument("--replay-limit", type=int, default=None, help="Limit replay to last N bars")
    parser.add_argument("--test-symbol", type=str, default=None, help="Symbol for test order (e.g., GLD)")
    parser.add_argument("--test-expiration", type=str, default=None, help="Expiration date YYYY-MM-DD")
    parser.add_argument("--test-strike", type=float, default=None, help="Strike price")
    parser.add_argument("--test-option-type", type=str, default=None, help="CALL or PUT")
    parser.add_argument("--test-asset", type=str, default=None, help="OPTION or STOCK")
    parser.add_argument("--test-qty", type=int, default=1, help="Contracts quantity")
    parser.add_argument("--test-limit", type=float, default=None, help="Limit price")
    parser.add_argument(
        "--auto-contract",
        action="store_true",
        help="Auto-pick a valid contract from the latest options chain for test orders",
    )
    args = parser.parse_args()

    config_path = Path("config.json")
    config = load_config(config_path)
    app = PlannerApp(config)
    webull_mode = (args.webull_mode or config.get("execution", {}).get("mode") or "paper").strip().lower()

    if args.record_pnl is not None:
        journal = EventJournal(config)
        journal.log_event("pnl_update", {"realized_pnl": args.record_pnl})
        print(f"Recorded PnL update: {args.record_pnl:.2f}")
        return 0

    if args.replay:
        from .replay import ReplayConfig, run_replay

        if not args.replay_bars or not args.replay_symbol:
            print("Replay requires --replay-bars and --replay-symbol.")
            return 1
        replay_cfg = ReplayConfig(
            bars_path=Path(args.replay_bars),
            chain_path=Path(args.replay_chain) if args.replay_chain else None,
            symbol=args.replay_symbol,
            limit=args.replay_limit,
        )
        summary = run_replay(config, replay_cfg)
        print(summary)
        return 0

    if args.list_paper_accounts:
        try:
            result = list_webull_paper_accounts(config)
            print(format_account_list(result))
        except Exception as exc:
            print(f"Failed to list paper accounts: {exc}")
            return 1
        return 0

    if args.show_buying_power:
        from .execution_adapter import format_balance_summary, list_webull_account_balance

        try:
            result = list_webull_account_balance(
                config,
                account_id=args.account_id,
                mode=webull_mode,
            )
            print(format_balance_summary(result))
        except Exception as exc:
            print(f"Failed to fetch account balance: {exc}")
            return 1
        return 0

    if args.list_open_orders or args.cancel_all_open or args.cancel_order:
        from .execution_adapter import (
            cancel_webull_order,
            format_order_list,
            list_webull_order_history,
        )

        try:
            orders = list_webull_order_history(
                config,
                account_id=args.account_id,
                mode=webull_mode,
            )
            print(format_order_list(orders))
        except Exception as exc:
            print(f"Failed to list orders: {exc}")
            return 1

        if args.cancel_order:
            try:
                result = cancel_webull_order(
                    config,
                    args.cancel_order,
                    account_id=args.account_id,
                    mode=webull_mode,
                )
                print(result)
            except Exception as exc:
                print(f"Failed to cancel order: {exc}")
                return 1
            return 0

        if args.cancel_all_open:
            cancelled = 0
            for client_order_id in orders.get("open_order_ids", []):
                try:
                    cancel_webull_order(
                        config,
                        client_order_id,
                        account_id=args.account_id,
                        mode=webull_mode,
                    )
                    cancelled += 1
                except Exception as exc:
                    print(f"Failed to cancel {client_order_id}: {exc}")
            print(f"Requested cancel for {cancelled} orders.")
            return 0
        return 0

    if args.analyze_webull_trades:
        from .execution_adapter import (
            analyze_webull_trade_history,
            fetch_webull_trade_history,
            format_webull_trade_analysis,
        )

        try:
            start_date = _normalize_iso_date(args.trade_start_date, "--trade-start-date")
            end_date = _normalize_iso_date(args.trade_end_date, "--trade-end-date")
            if start_date and end_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                if start_dt > end_dt:
                    print("--trade-start-date must be on or before --trade-end-date.")
                    return 1

            history = fetch_webull_trade_history(
                config,
                account_id=args.account_id,
                mode=webull_mode,
                start_date=start_date,
                end_date=end_date,
                page_size=args.trade_page_size,
                max_pages=args.trade_max_pages,
            )
            analysis = analyze_webull_trade_history(history)
            print(format_webull_trade_analysis(analysis))

            if args.trade_report_path:
                report = {
                    "history": history,
                    "analysis": analysis,
                }
                output_path = Path(args.trade_report_path)
                output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(f"Saved trade report to: {output_path}")
        except Exception as exc:
            print(f"Failed to analyze Webull trades: {exc}")
            return 1
        return 0

    if args.submit_test_order:
        from .data.provider_factory import create_market_data_provider
        from .data.yahoo import YahooMarketDataProvider
        from .execution_factory import create_execution_adapter
        from .order_manager import OrderManager
        from .services import OptionInstrumentService

        exec_cfg = config.get("execution", {})
        test_cfg = exec_cfg.get("test_order", {})
        symbol = args.test_symbol or test_cfg.get("symbol")
        option_type = args.test_option_type or test_cfg.get("option_type", "CALL")
        asset_class = (args.test_asset or test_cfg.get("asset_class", "OPTION")).upper()
        qty = args.test_qty or test_cfg.get("qty", 1)
        limit_price = args.test_limit or test_cfg.get("limit_price")

        expiration = args.test_expiration or test_cfg.get("expiration")
        strike = args.test_strike or test_cfg.get("strike")

        if args.auto_contract and asset_class != "STOCK":
            try:
                provider, _ = create_market_data_provider(config)
                interval = config.get("strategy", {}).get("interval", "5m")
                history_period = config.get("strategy", {}).get("history_period", "1d")
                try:
                    df = provider.get_intraday_bars(symbol, history_period, interval)
                except Exception:
                    df = YahooMarketDataProvider().get_intraday_bars(symbol, "5d", interval)
                try:
                    chain = provider.get_options_chain(
                        symbol,
                        config.get("options", {}).get("target_dte", 1),
                    )
                except Exception:
                    chain = YahooMarketDataProvider().get_options_chain(
                        symbol,
                        config.get("options", {}).get("target_dte", 1),
                    )
                selector = OptionInstrumentService(config.get("options", {}))
                selection = selector.select_contract(
                    symbol,
                    chain,
                    option_type.upper(),
                    float(df["Close"].iloc[-1]),
                    datetime.now(timezone.utc).isoformat(),
                )
                if not selection.option_contract:
                    print("Failed to auto-pick a contract. Check options chain availability.")
                    return 1
                option = selection.option_contract
                expiration = option.expiration
                strike = option.strike
                option_type = option.option_type
                if limit_price is None:
                    limit_price = option.mid
            except Exception as exc:
                print(f"Auto-contract selection failed: {exc}")
                return 1

        if asset_class == "STOCK":
            if not symbol:
                print("Missing test order fields. Provide --test-symbol for stock orders.")
                return 1
        else:
            if not symbol or not expiration or strike is None:
                print("Missing test order fields. Provide --test-symbol --test-expiration --test-strike or use --auto-contract.")
                return 1

        adapter = create_execution_adapter(config, EventJournal(config))
        order_manager = OrderManager(adapter, EventJournal(config))
        payload = {
            "symbol": symbol,
            "asset_class": asset_class,
            "side": "BUY",
            "direction": option_type.upper(),
            "contracts": int(qty),
            "qty": int(qty),
            "order_type": (exec_cfg.get("order_type") or "LIMIT").upper(),
            "limit_price": limit_price,
            "tif": exec_cfg.get("tif", "DAY"),
            "expiration": expiration,
            "strike": float(strike) if strike is not None else None,
            "option_type": option_type.upper(),
        }
        result = order_manager.submit_bracket_intent(payload)
        print(result)
        return 0

    web_enabled = args.web or config.get("web", {}).get("enabled", False)
    if web_enabled:
        interval = args.interval
        if interval is None:
            interval = config.get("web", {}).get(
                "update_seconds", config.get("watch", {}).get("interval_seconds", 300)
            )
        host = args.host or config.get("web", {}).get("host", "127.0.0.1")
        port = args.port or config.get("web", {}).get("port", 8000)
        start_dashboard(app, host, port, max(5, interval))
        return 0

    watch_enabled = args.watch or config.get("watch", {}).get("enabled", False)
    interval = args.interval
    if interval is None:
        interval = config.get("watch", {}).get("interval_seconds", 300)

    if not watch_enabled:
        plans, errors = app.run(log_to_journal=True)
        for error in errors:
            print(error)
        print_plans(plans, config)
        return 0

    try:
        while True:
            plans, errors = app.run(log_to_journal=True)
            for error in errors:
                print(error)
            print_plans(plans, config)
            time.sleep(max(5, interval))
    except KeyboardInterrupt:
        print("Stopping watch mode.")
        return 0


def _normalize_iso_date(value: str | None, flag_name: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError(f"{flag_name} must be in YYYY-MM-DD format.") from exc
    return parsed.strftime("%Y-%m-%d")


if __name__ == "__main__":
    raise SystemExit(main())
