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
"""

from datetime import datetime
from typing import Dict, List, Optional

from src.utils.logger import LOGGER


def tx_history(limit: int = 10):
    """Print one section per payment system this node offers.

    One section each rather than one merged list: two payment systems are two chains
    with their own money and their own confirmation counts, and interleaving them by
    timestamp would put figures in different units next to each other.
    """
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
        return

    # Resolved once for the whole page, not per section and not per transaction: both
    # are a single query, and the second (every deposit token this node ever issued) is
    # the only way an incoming payment can be attributed at all.
    clients_by_token = _clients_by_deposit_token()

    for index, contract in enumerate(offered):
        if index:
            print()
        _display_contract_history(contract, clients_by_token, limit)


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
            lines.append("Purpose: donation")

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
