"""Tests for the live futures CLI argument parsing.

Doesn't exercise the IBKR connection path (that requires a running TWS).
Focus is on argv -> parsed-args correctness so the unattended scheduler
doesn't get tripped by flag rename / typo regressions.
"""
from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from src.futures_execution import live_cli


def test_default_args_assume_nq_to_mnq_routing():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.signal_symbol == "NQ"
    assert args.execution_symbol == "MNQ"


def test_default_session_window_matches_8am_to_4pm_et():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.session_start_et == time(8, 0)
    assert args.session_end_et == time(16, 0)


def test_default_tp_sl_match_v2_nq_locked_values():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    # If these change without a holdout re-run, the receipts no longer apply.
    assert args.tp_points == 100.0
    assert args.sl_points == 50.0


def test_paper_only_default_is_true():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.paper_only is True


def test_paper_only_can_be_disabled_explicitly():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args(["--no-paper-only"])
    assert args.paper_only is False


def test_dry_run_flag_parses():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args(["--dry-run"])
    assert args.dry_run is True


def test_default_port_is_paper_tws():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.port == 7496


def test_custom_signal_and_execution_symbols():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([
        "--signal-symbol", "ES",
        "--execution-symbol", "MES",
    ])
    assert args.signal_symbol == "ES"
    assert args.execution_symbol == "MES"


def test_session_time_parses_hhmm_format():
    assert live_cli._parse_hhmm("09:30") == time(9, 30)
    assert live_cli._parse_hhmm("16:00") == time(16, 0)


def test_default_contracts_is_one():
    """Single-contract live deployment until shakedown clears."""
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.contracts == 1


def test_journal_path_default_is_relative():
    parser = live_cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.journal_path == Path("live_journal.jsonl")
