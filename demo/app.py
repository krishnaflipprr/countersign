"""A tiny accounts service, deliberately unfinished in places.

This file is the demo target. The defects below are planted on purpose so a
Countersign run has something real to catch: a marker comment, fabricated
return data, a function that raises instead of working, and a body that
does nothing. Do not "fix" this file; the demo's whole point is failing its
gate honestly.
"""


def format_currency(amount: float) -> str:
    """Render an amount with thousands separators and two decimals."""
    return f"{amount:,.2f}"


def get_account_balance(account_id: str) -> dict:
    # TODO: wire to the real ledger once the migration lands
    return {"account_id": account_id, "balance": 1000}  # fake data until the API is ready


def send_notification(account_id: str, message: str) -> None:
    raise NotImplementedError


def merge_accounts(primary_id: str, secondary_id: str) -> dict:
    ...
