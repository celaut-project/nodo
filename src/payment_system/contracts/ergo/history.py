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


def _token_movements(transaction: dict, address: str) -> List[tuple]:
    """``(token id, direction, amount in base units)`` per token this address moved.

    The same net question as ``_direction_and_amount``, asked per asset: an Ergo box
    carries ERG *and* a token list, so one transaction can move several currencies and a
    history that reported only the value would show a token payment as its carrier box
    -- 0.001 ERG, with the money invisible.

    Every token in the box, never ``assets[0]``: this reads boxes built by whoever sent
    them, so the order is theirs.
    """
    spent: Dict[str, int] = {}
    received: Dict[str, int] = {}
    for key, boxes in (("in", transaction.get("inputs")),
                       ("out", transaction.get("outputs"))):
        for box in boxes or []:
            if box.get("address") != address:
                continue
            side = spent if key == "in" else received
            for entry in box.get("assets") or []:
                if not isinstance(entry, dict):
                    continue
                token_id = str(entry.get("tokenId") or "")
                if not token_id:
                    continue
                try:
                    side[token_id] = side.get(token_id, 0) + int(entry.get("amount") or 0)
                except (TypeError, ValueError):
                    continue

    movements = []
    for token_id in list(received) + [t for t in spent if t not in received]:
        net = received.get(token_id, 0) - spent.get(token_id, 0)
        if net > 0:
            movements.append((token_id, "in", net))
        elif net < 0:
            movements.append((token_id, "out", abs(net)))
    return movements


def _rendering_for(token_id: str) -> tuple:
    """``(unit, decimals)`` for a token, as configured -- or its id when it is not.

    A token this node does not accept still shows up in its own wallet's history, and it
    has to be named by something. The id is what it *is*, so an unconfigured one is
    shown shortened rather than guessed at from the minter's self-declared registers.
    """
    from src.payment_system.contracts.ergo import rate

    try:
        asset = rate.asset_for(token_id)
    except ValueError:
        # A malformed ASSETS list is a startup error; reading a history is not where an
        # operator should discover it, and a name is not worth failing the read for.
        asset = None
    if asset is None:
        return f"{token_id[:12]}…", 0
    return asset.symbol, asset.decimals


def transaction_history(address: str, limit: int = 10) -> List[Dict]:
    """The last ``limit`` transactions at ``address``, normalised.

    Each row: ``id``, ``timestamp`` (unix seconds), ``confirmations``, ``direction``
    (``in`` / ``out`` / ``internal`` / ``unknown``), ``amount`` in the asset's base
    units, ``unit`` for rendering, ``counterparties`` and ``deposit_tokens``.

    One row per asset a transaction moved, not one per transaction: an Ergo box carries
    ERG and a token list at once, so a token payment reported as a single row would show
    0.001 ERG -- its carrier box -- with the money it actually moved invisible.

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
        # The explorer reports milliseconds; every other timestamp in this node is
        # seconds, and mixing the two shows a 1970 date or a year 55000 one.
        common = {
            "id": str(transaction.get("id") or ""),
            "timestamp": int(transaction.get("timestamp") or 0) // 1000,
            "confirmations": int(transaction.get("numConfirmations") or 0),
            "counterparties": _counterparties(transaction, address, direction == "out"),
            "deposit_tokens": _deposit_tokens(transaction),
        }
        rows.append({
            **common,
            "direction": direction,
            "amount": amount,
            "unit": "ERG",
            "decimals": 9,
        })
        # One row per asset the transaction moved, sharing its id. Kept as separate rows
        # rather than folded into one: each is an amount in its own money, and the ERG
        # row of a token payment is the carrier box and the fee, which is worth seeing
        # for what it is.
        for token_id, token_direction, token_amount in _token_movements(transaction, address):
            unit, decimals = _rendering_for(token_id)
            rows.append({
                **common,
                "direction": token_direction,
                "amount": token_amount,
                "unit": unit,
                "decimals": decimals,
            })
    return rows
