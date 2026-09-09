"""Recent transactions at this node's Ergo wallet, in the shape any ledger can answer.

Every chain-shaped part of `nodo tx_history` used to live in the command: it read the
Ergo explorer, walked Ergo boxes to decide a direction, and pulled two private helpers
out of `interface.py` to render nanoERG. So "transaction history" meant "Ergo's
transaction history", and a second payment system had nowhere to appear.

This is Ergo's answer to the question, normalised. The command renders rows and joins
them to what this node recorded locally; deciding what a row *is* belongs to the chain
that produced it.

Light on purpose, like `rate.py` and `donation_scan.py`: reading history is a read, and
it must not need a wallet, a signature or a JVM. The explorer URL comes from the
configured network rather than from an AppKit handle for exactly that reason.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import requests

from src.reputation_system.contracts.ergo.utils import explorer_api_url
from src.utils.logger import LOGGER

TIMEOUT_SECONDS = 30


def _direction_and_amount(transaction: dict, address: str) -> tuple:
    """``(direction, amount in nanoERG)`` for ``address`` in ``transaction``.

    Ergo has no "sender" field: a transaction spends boxes and creates boxes, so which
    side this node is on is a question about which of them carry its address. Change
    goes back to the sender, so a transaction can legitimately be on both sides at
    once, and then the net figure is the one that means anything.
    """
    spent = sum(
        int(box.get("value") or 0) for box in transaction.get("inputs") or []
        if box.get("address") == address
    )
    received = sum(
        int(box.get("value") or 0) for box in transaction.get("outputs") or []
        if box.get("address") == address
    )
    if spent and received:
        net = received - spent
        if net > 0:
            return "in", net
        if net < 0:
            return "out", abs(net)
        return "internal", 0
    if spent:
        return "out", spent
    if received:
        return "in", received
    # The explorer sometimes hands back inputs without addresses, and then nothing here
    # can tell. Said as unknown rather than guessed at; the command's local payment row
    # settles the direction when there is one.
    return "unknown", 0


def _counterparties(transaction: dict, address: str, outgoing: bool) -> List[str]:
    """Every address on the other side, ours excluded.

    Change goes back to the sender, so an outgoing transaction lists our own address
    among its outputs; dropping it is what leaves the recipient.
    """
    boxes = transaction.get("outputs" if outgoing else "inputs") or []
    seen: List[str] = []
    for box in boxes:
        other = box.get("address")
        if other and other != address and other not in seen:
            seen.append(other)
    return seen


def _deposit_tokens(transaction: dict) -> List[str]:
    """Deposit tokens carried in R4 of this transaction's outputs.

    Mirrors what `payment_process_validator` reads: the register holds the token as
    UTF-8 bytes, rendered by the explorer as hex. Anything that does not decode is some
    other application's register and is skipped.
    """
    tokens: List[str] = []
    for box in transaction.get("outputs") or []:
        registers = box.get("additionalRegisters") or {}
        rendered = (registers.get("R4") or {}).get("renderedValue")
        if not rendered:
            continue
        try:
            tokens.append(bytes.fromhex(rendered).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
    return tokens


def transaction_history(address: str, limit: int = 10) -> List[Dict]:
    """The last ``limit`` transactions at ``address``, normalised.

    Each row: ``id``, ``timestamp`` (unix seconds), ``confirmations``, ``direction``
    (``in`` / ``out`` / ``internal`` / ``unknown``), ``amount`` in the asset's base
    units, ``unit`` for rendering, ``counterparties`` and ``deposit_tokens``.

    Raises on a failure to read, so the command can say "could not look" rather than
    printing an empty history that reads as "nothing ever happened here".
    """
    url = f"{explorer_api_url().rstrip('/')}/api/v1/addresses/{address}/transactions"
    try:
        response = requests.get(
            url, params={"offset": 0, "limit": int(limit)}, timeout=TIMEOUT_SECONDS
        )
    except requests.exceptions.RequestException as exc:
        raise ValueError(f"could not reach the Ergo explorer: {type(exc).__name__}") from None
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        raise ValueError(f"the Ergo explorer answered HTTP {response.status_code}")
    try:
        items = (response.json() or {}).get("items") or []
    except ValueError:
        raise ValueError("the Ergo explorer returned unreadable JSON") from None

    rows: List[Dict] = []
    for transaction in items:
        direction, amount = _direction_and_amount(transaction, address)
        rows.append({
            "id": str(transaction.get("id") or ""),
            # The explorer reports milliseconds; every other timestamp in this node is
            # seconds, and mixing the two shows a 1970 date or a year 55000 one.
            "timestamp": int(transaction.get("timestamp") or 0) // 1000,
            "confirmations": int(transaction.get("numConfirmations") or 0),
            "direction": direction,
            "amount": amount,
            "unit": "ERG",
            "decimals": 9,
            "counterparties": _counterparties(transaction, address, direction == "out"),
            "deposit_tokens": _deposit_tokens(transaction),
        })
    return rows
