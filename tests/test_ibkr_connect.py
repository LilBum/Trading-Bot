from unittest.mock import MagicMock

import pytest

from src.futures_execution.ibkr import IBKRConnectionConfig
from src.futures_execution.ibkr_connect import (
    NonPaperAccountError,
    connect_with_safety_check,
    is_paper_account,
    verify_paper_only,
)


# ----- is_paper_account -------------------------------------------------


def test_paper_account_recognized_by_du_prefix():
    assert is_paper_account("DUQ822130") is True
    assert is_paper_account("DU1234567") is True


def test_live_account_not_recognized_as_paper():
    assert is_paper_account("U1234567") is False
    assert is_paper_account("F1234567") is False


def test_empty_account_not_paper():
    assert is_paper_account("") is False
    assert is_paper_account(None) is False  # type: ignore[arg-type]


# ----- verify_paper_only ------------------------------------------------


def test_verify_paper_only_passes_for_all_paper():
    verify_paper_only(["DU111", "DU222"])  # no raise


def test_verify_paper_only_raises_on_any_live_account():
    with pytest.raises(NonPaperAccountError, match="non-paper accounts"):
        verify_paper_only(["DU111", "U999"])


def test_verify_paper_only_raises_on_all_live_accounts():
    with pytest.raises(NonPaperAccountError):
        verify_paper_only(["U111", "U222"])


# ----- connect_with_safety_check ----------------------------------------


def _stub_ib(accounts):
    ib = MagicMock()
    ib.managedAccounts.return_value = accounts
    return ib


def test_connect_returns_paper_accounts_in_paper_mode():
    ib = _stub_ib(["DUQ822130"])
    cfg = IBKRConnectionConfig(port=7496)
    out_ib, accounts = connect_with_safety_check(cfg, paper_only=True, ib_client=ib)
    assert out_ib is ib
    assert accounts == ["DUQ822130"]
    ib.connect.assert_called_once()


def test_connect_disconnects_and_raises_on_live_account_in_paper_mode():
    ib = _stub_ib(["U1234567"])  # live account
    cfg = IBKRConnectionConfig(port=7496)
    with pytest.raises(NonPaperAccountError):
        connect_with_safety_check(cfg, paper_only=True, ib_client=ib)
    ib.disconnect.assert_called_once()


def test_connect_disconnects_and_raises_when_no_managed_accounts():
    ib = _stub_ib([])
    cfg = IBKRConnectionConfig(port=7496)
    with pytest.raises(RuntimeError, match="no managed accounts"):
        connect_with_safety_check(cfg, paper_only=True, ib_client=ib)
    ib.disconnect.assert_called_once()


def test_connect_passes_through_when_paper_only_disabled():
    """If LO explicitly disables paper_only, live accounts pass through."""
    ib = _stub_ib(["U9999999"])
    cfg = IBKRConnectionConfig(port=7496)
    out_ib, accounts = connect_with_safety_check(cfg, paper_only=False, ib_client=ib)
    assert accounts == ["U9999999"]
    ib.disconnect.assert_not_called()


def test_connect_uses_config_host_port_clientid():
    ib = _stub_ib(["DUQ822130"])
    cfg = IBKRConnectionConfig(host="10.0.0.5", port=7497, client_id=42)
    connect_with_safety_check(cfg, ib_client=ib)
    args, kwargs = ib.connect.call_args
    assert kwargs["host"] == "10.0.0.5"
    assert kwargs["port"] == 7497
    assert kwargs["clientId"] == 42


def test_connect_disconnects_if_managed_accounts_query_errors():
    ib = MagicMock()
    ib.managedAccounts.side_effect = RuntimeError("API not initialized")
    cfg = IBKRConnectionConfig()
    with pytest.raises(RuntimeError):
        connect_with_safety_check(cfg, ib_client=ib)
    ib.disconnect.assert_called_once()
