"""IBKR connection helper with paper-only safety check.

Wraps `ib_insync.IB.connect()` with a hard verification that the connected
account is a paper account (account ID starts with "DU"). If `paper_only`
is True (default), refuses to operate against any live account — disconnects
immediately and raises. This is the last line of defense against an
accidental live-account connection during shakedown.

Usage:
    ib, accounts = connect_with_safety_check(IBKRConnectionConfig(port=7496))
    print(accounts)  # ['DUQ822130']
    ...
    ib.disconnect()
"""

from __future__ import annotations

import asyncio as _asyncio

from src.futures_execution.ibkr import IBKRConnectionConfig

try:
    _asyncio.get_event_loop()
except RuntimeError:
    _asyncio.set_event_loop(_asyncio.new_event_loop())

try:  # pragma: no cover
    from ib_insync import IB
except ImportError:  # pragma: no cover
    IB = None


PAPER_ACCOUNT_PREFIX = "DU"


class NonPaperAccountError(RuntimeError):
    """Raised when paper-only mode detects a non-paper managed account."""


def is_paper_account(account_id: str) -> bool:
    """An IBKR account is a paper account iff its ID starts with 'DU'."""
    return bool(account_id and account_id.startswith(PAPER_ACCOUNT_PREFIX))


def verify_paper_only(accounts: list[str]) -> None:
    """Raise NonPaperAccountError if any account isn't a paper account."""
    non_paper = [a for a in accounts if not is_paper_account(a)]
    if non_paper:
        raise NonPaperAccountError(
            f"Refusing to operate: non-paper accounts detected: {non_paper}. "
            "Either disconnect from live TWS and reconnect to paper, or pass "
            "paper_only=False (DON'T do this until live deployment is approved)."
        )


def connect_with_safety_check(
    config: IBKRConnectionConfig,
    paper_only: bool = True,
    ib_client=None,
):
    """Connect to IBKR Gateway/TWS with paper safety check.

    Returns (ib_client, managed_accounts). On non-paper detection in
    paper_only mode, disconnects and raises NonPaperAccountError.

    `ib_client` may be injected for testing; otherwise a fresh `ib_insync.IB`
    is created.
    """
    if ib_client is None:
        if IB is None:
            raise RuntimeError("ib_insync is not installed.")
        ib_client = IB()

    ib_client.connect(
        host=config.host,
        port=config.port,
        clientId=config.client_id,
        timeout=config.connect_timeout_seconds,
    )

    try:
        accounts = list(ib_client.managedAccounts())
    except Exception:
        ib_client.disconnect()
        raise

    if not accounts:
        ib_client.disconnect()
        raise RuntimeError(
            "Connected but no managed accounts returned. Is the TWS login active?"
        )

    if paper_only:
        try:
            verify_paper_only(accounts)
        except NonPaperAccountError:
            ib_client.disconnect()
            raise

    return ib_client, accounts
