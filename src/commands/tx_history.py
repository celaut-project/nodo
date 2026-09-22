"""``nodo tx_history`` -- what this node has paid and been paid, per payment system.

Every chain-shaped part of this used to live here: it read the Ergo explorer, walked
Ergo boxes to decide whether a transaction was incoming, and reached into
`contracts.ergo.interface`'s privates to render nanoERG. So "transaction history" meant
"Ergo's", and a second payment system had nowhere to appear.

Now each contract answers for its own chain, in one normalised shape
(``contracts/<ledger>/history.py`` for Ergo, `listtransactions` for Bitcoin), and what
is left here is the part that is nobody's chain: **who** was on the other side. The
chain knows addresses; only this node knows which peer an address belonged to when it
was paid, and which client a deposit token was issued to.

Two audiences, one computation, the same shape as ``nodo reputation`` and ``nodo
donations``: a person reads the printed form, and the TUI reads ``--json``. The TUI's
PRICING page draws a block per payment system from :func:`report`, so what it shows is
what this command prints rather than a second walk of the same chains -- and a second
implementation of "is this ledger reachable" would be a second answer.
"""

import json
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from src.utils.logger import LOGGER

# How many transactions a JSON report carries per ledger. Smaller than the printed
# command's default: the reader is a panel a few rows tall, and every extra row costs
# an explorer page.
JSON_LIMIT = 5


def tx_history(limit: int = 10, argv: Optional[List[str]] = None) -> bool:
    """Print one section per payment system this node offers.

    One section each rather than one merged list: two payment systems are two chains
    with their own money and their own confirmation counts, and interleaving them by
    timestamp would put figures in different units next to each other.

    ``nodo tx_history [--json]``. Returns whether every offered ledger answered, which
    is what the exit status is.
    """
    argv = list(argv or [])
    if "--json" in argv:
        json.dump(report(limit=JSON_LIMIT), sys.stdout)
        print()
        sys.stdout.flush()
        return True

    print("Transaction History")
    print("=" * 50)

    from src.payment_system.contracts.registry import attribute, contracts

    offered = [
        contract for contract in contracts().values()
        if not attribute(contract, "is_demo")
        and callable(getattr(contract, "transaction_history", None))
    ]
    if not offered:
        print(
            "No payment system can report a history. Configure a ledger under "
            "`ledgers:` and check that its runtime is reachable."
        )
        sys.stdout.flush()
        return True

    # Resolved once for the whole page, not per section and not per transaction: both
    # are a single query, and the second (every deposit token this node ever issued) is
    # the only way an incoming payment can be attributed at all.
    clients_by_token = _clients_by_deposit_token()

    for index, contract in enumerate(offered):
        if index:
            print()
        _display_contract_history(contract, clients_by_token, limit)
    sys.stdout.flush()
    return True


def report(limit: int = JSON_LIMIT, now: Optional[int] = None) -> dict:
    """Everything this command knows, as the JSON the TUI reads.

    One entry per **candidate** ledger, not per offered one, and the demo contract is
    left out of both. "Configured but not usable" and "never configured" are different
    facts with different fixes -- an unset `MU_PER_SATOSHI` against a `ledgers.bitcoin`
    block nobody wrote -- and a report listing only what works answers "where is my
    ledger" with nothing at all.

    Never raises. Every per-ledger read is fenced: an explorer that times out costs that
    ledger its transaction list and nothing else, because the rate and the address beside
    it are config reads that are still true.
    """
    from src.payment_system.contracts.registry import (
        CANDIDATES, attribute, contracts, is_configured,
    )

    offered = {
        getattr(contract, "LEDGER", ""): contract
        for contract in contracts().values()
        if not attribute(contract, "is_demo")
    }
    clients_by_token = _clients_by_deposit_token() if offered else {}

    ledgers = []
    for candidate in CANDIDATES:
        if candidate.name == "simulated":
            continue
        entry = {
            "ledger": candidate.name,
            "configured": bool(_safely(is_configured, candidate.name, default=False)),
            "offered": candidate.name in offered,
            "unavailable_reason": "",
            "address": "",
            "rate": _rate(candidate),
            "transactions": [],
            "history_error": "",
        }
        contract = offered.get(candidate.name)
        if contract is None:
            entry["unavailable_reason"] = _unavailable_reason(candidate, entry["configured"])
            ledgers.append(entry)
            continue
        entry["address"] = _safely(contract.get_wallet_address, default="") or ""
        try:
            entry["transactions"], entry["history_error"] = _transactions(
                contract, clients_by_token, limit
            )
        except Exception as error:
            entry["history_error"] = str(error)
        ledgers.append(entry)

    return {"ledgers": ledgers, "read_at": int(time.time()) if now is None else int(now)}


def _safely(call, *args, default=None):
    """``call(*args)``, or ``default`` when the chain, the config or the JVM says no.

    Broad on purpose. This is a report: a ledger that cannot answer one question is
    still worth every other line about it, and each caller decides what a missing
    answer looks like.
    """
    try:
        return call(*args)
    except Exception as e:
        LOGGER(f"tx_history report: {call.__name__} failed: {e}")
        return default


def _rate(candidate) -> dict:
    """What one MU is worth on this ledger, from its own light rate module.

    The rate module rather than the contract: it is the one part of a payment system
    that is readable without a JVM or a socket, which is exactly the case this has to
    report on -- an operator whose Bitcoin is unusable *because* of its rate needs to
    read the rate.
    """
    from importlib import import_module

    if not candidate.rate_path:
        return {}
    module = _safely(import_module, candidate.rate_path)
    if module is None:
        return {}
    units = _safely(module.display_units, default={}) or {}
    native = units.get(getattr(module, "UNIT_NAME", ""), {})
    return {
        "key": getattr(module, "RATE_KEY", ""),
        "symbol": getattr(module, "UNIT_SYMBOL", ""),
        "decimals": getattr(module, "UNIT_DECIMALS", 0),
        # Per BASE unit (nanoERG, satoshi): the figure the config key holds, so what is
        # shown is what an edit changes. `mu_per_unit` is derived from it.
        "mu_per_base_unit": _rate_text(module),
        "mu_per_unit": str(native.get("MU_PER_UNIT", "")),
        "reason": _safely(getattr(module, "rate_reason", lambda: None), default=None) or "",
    }


def _rate_text(module) -> str:
    """The configured per-base-unit rate as a string, or ``""`` when it is unusable.

    Read through the module's own accessor -- ``mu_per_nanoerg`` / ``mu_per_satoshi`` --
    rather than off the config key, so the validation that decides whether a rate may be
    used at all is the validation this reports.
    """
    for name in ("mu_per_nanoerg", "mu_per_satoshi"):
        accessor = getattr(module, name, None)
        if callable(accessor):
            value = _safely(accessor, default=None)
            return "" if value is None else str(value)
    return ""


def _unavailable_reason(candidate, configured: bool) -> str:
    """Why a configured ledger is not being offered, in the operator's own terms.

    The contract's own ``unavailable_reason`` where there is one -- it is the sentence
    the registry logs and it names the key to fix. A ledger nobody configured gets a
    plain statement of that rather than a reason, because there is nothing wrong.
    """
    from importlib import import_module

    if not configured:
        return f"ledgers.{candidate.name} is not configured, so this node does not offer it."
    module = _safely(import_module, candidate.module_path)
    if module is None:
        return (
            f"The {candidate.name} contract could not be imported, so this node is not "
            "offering it. See app.log for what it said on the way past."
        )
    reason = getattr(module, "unavailable_reason", None)
    if callable(reason):
        return str(_safely(reason, default="") or "")
    return ""


def _transactions(contract, clients_by_token: Dict[str, str], limit: int):
    """``(rows, error)`` for one ledger, each row already joined to what this node knows.

    An error rather than an empty list when the read fails: "could not look" is not
    "nothing happened", and a wallet nobody has ever used is a fact worth being able
    to state.
    """
    history = getattr(contract, "transaction_history", None)
    if not callable(history):
        return [], (
            f"The {getattr(contract, 'LEDGER', '?')} contract does not report a "
            "transaction history."
        )
    try:
        rows = history(limit=limit) or []
    except Exception as e:
        return [], str(e)

    payments = _payments_by_tx_id(rows)
    return [
        {
            "id": row.get("id") or "",
            "amount": _format_amount(row),
            "timestamp": int(row.get("timestamp") or 0),
            "confirmations": int(row.get("confirmations") or 0),
            "direction": row.get("direction") or "unknown",
            "counterparty": _counterparty_lines(row, payments, clients_by_token),
        }
        for row in rows
    ], ""


def _display_contract_history(contract, clients_by_token: Dict[str, str], limit: int):
    """One payment system's recent transactions."""
    ledger = getattr(contract, "LEDGER", "?")
    try:
        address = contract.get_wallet_address()
    except Exception as e:
        print(f"[{ledger}] - wallet unavailable: {e}")
        return

    print(f"[{ledger}] - Address: {address}")
    print("-" * 60)

    try:
        rows = contract.transaction_history(limit=limit)
    except Exception as e:
        # "Could not look" is not "nothing happened": an empty list here would read as
        # a wallet nobody has ever used.
        LOGGER(f"Error fetching {ledger} transactions: {e}")
        print(f"Could not read the {ledger} history: {e}")
        return

    if not rows:
        print("No recent transactions found.")
        return

    payments = _payments_by_tx_id(rows)
    for index, row in enumerate(rows):
        _display_transaction(row, payments, clients_by_token)
        if index < len(rows) - 1:
            print()


def _display_transaction(row: Dict, payments: Dict[str, Dict],
                         clients_by_token: Dict[str, str]):
    """One normalised row, and whoever this node can name on the other side of it."""
    try:
        print(f"Transaction ID: {row.get('id') or 'N/A'}")
        print(f"Amount: {_format_amount(row)}")
        print(f"Timestamp: {_format_timestamp(row.get('timestamp') or 0)}")
        print(f"Confirmations: {row.get('confirmations', 0)}")
        print(f"Direction: {_DIRECTIONS.get(row.get('direction'), 'Unknown')}")
        for line in _counterparty_lines(row, payments, clients_by_token):
            print(line)
    except Exception as e:
        LOGGER(f"Error displaying transaction: {e}")
        print(f"Error displaying transaction: {e}")


_DIRECTIONS = {
    "in": "Incoming",
    "out": "Outgoing",
    # Both sides of the same transaction: change coming back, or a wallet paying itself.
    "internal": "Internal",
    "unknown": "Unknown",
}


def _format_amount(row: Dict) -> str:
    """A base-unit integer as its own money, never converted to MU.

    What a chain moved is denominated by that chain. Rendering it in the operator's
    display unit would put a converted figure next to a confirmation count, and the two
    would be describing different things.
    """
    amount = int(row.get("amount") or 0)
    decimals = int(row.get("decimals") or 0)
    unit = row.get("unit") or ""
    if decimals <= 0:
        return f"{amount} {unit}".strip()
    whole = amount / (10 ** decimals)
    return f"{whole:.{decimals}f} {unit}".strip()


def _counterparty_lines(row: Dict, payments: Dict[str, Dict],
                        clients_by_token: Dict[str, str]) -> List[str]:
    """Who was on the other side, named when this node can name them.

    Three sources, most trustworthy first: the payment this node recorded when it made
    it (exact -- it holds the peer id), the deposit token the transaction carries
    (exact -- it holds the client id), and failing both the raw address, which is still
    more than nothing.
    """
    lines: List[str] = []
    outgoing = row.get("direction") == "out"

    # Only outgoing rows can match here: an incoming payment is recorded without a
    # transaction id, because the box or output proving it is not the transaction that
    # made it.
    payment = payments.get(row.get("id") or "")
    if payment:
        # A row means this node signed the transaction, which settles the direction more
        # firmly than reading the chain does -- a contract reports "unknown" whenever
        # the chain hands back inputs without addresses.
        outgoing = payment.get("direction", "out") == "out"
        if payment.get("peer_id"):
            lines.append(f"To: peer {payment['peer_id']}")
        if payment.get("purpose") == "donation":
            # `purpose` is what makes it a donation; the asset is what says which money
            # it was paid in. One tick can pay a debt in two assets, and both rows carry
            # the same transaction id and a deliberately ledger-neutral `amount_mu`.
            asset = payment.get("token_id") or ""
            lines.append(
                f"Purpose: donation ({asset})" if asset else "Purpose: donation"
            )

    if not outgoing:
        for token in row.get("deposit_tokens") or []:
            client_id = clients_by_token.get(token)
            if client_id:
                lines.append(f"From: client {client_id} (deposit token {token})")
            else:
                lines.append(f"From: an unknown deposit token {token}")
            break

    counterparties = [address for address in row.get("counterparties") or [] if address]
    if counterparties:
        label = "To address" if outgoing else "From address"
        lines.append(f"{label}: {', '.join(counterparties)}")
    elif not lines:
        lines.append("Counterparty: unknown")

    if payment and payment.get("status") == "unacknowledged":
        lines.append(
            "NOTE: this node never got an acknowledgement for this payment; the money "
            "left but no balance was credited."
        )
    return lines


def _payments_by_tx_id(transactions: List[Dict]) -> Dict[str, Dict]:
    """Local payment rows for the transactions on screen, keyed by transaction id.

    The chain knows addresses; only this node knows which peer an address belonged to
    when it was paid. A checkout with no such rows yet -- or a wallet with activity
    nodo never made -- just gets nothing back, and the raw address is shown instead.
    """
    try:
        from src.database.sql_connection import SQLConnection

        return SQLConnection().get_payments_by_tx_ids(
            [tx.get('id') for tx in transactions if tx.get('id')]
        )
    except Exception as e:
        LOGGER(f"Could not read local payment records: {str(e)}")
        return {}


def _clients_by_deposit_token() -> Dict[str, str]:
    """client id per deposit token, for attributing incoming payments.

    A client pays by putting its deposit token in R4 of the box it sends us -- that
    register is how the node validates the payment in the first place, so it is also
    the one honest way to say who a received transaction came from. The payer's own
    address says nothing: a client is not an address, and nothing on chain links them.
    """
    try:
        from src.database.sql_connection import SQLConnection

        return {
            token['id']: token['client_id']
            for token in SQLConnection().get_deposit_tokens()
            if token.get('client_id')
        }
    except Exception as e:
        LOGGER(f"Could not read deposit tokens: {str(e)}")
        return {}


def _format_timestamp(timestamp: int) -> str:
    """A unix timestamp in **seconds** as a person reads it.

    Seconds, not milliseconds. Each contract normalises its own chain's units before
    this sees them -- Ergo's explorer reports milliseconds -- because a page that
    divided by a thousand on behalf of one chain showed the other one a date in 1970.
    """
    try:
        seconds = int(timestamp)
        if seconds <= 0:
            return "N/A"
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Invalid timestamp"
