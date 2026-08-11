"""Pay ERG to a specific peer via the node's single-wallet flow (user-invoked dev command).

This reuses the node's *existing* single-wallet payment machinery — no crypto,
payment, or gRPC logic is reimplemented here:

  Pay -> :func:`src.payment_system.payment_process.increase_deposit_on_peer`,
         which drives
         :func:`src.payment_system.contracts.ergo.interface.process_payment`
         (build + sign + broadcast + wait for confirmations; the transaction
         URL is emitted to the node log) and then the ``Payable`` gRPC round-trip.

There is deliberately **no payer-side verify** step. The authoritative check —
:func:`...ergo.interface.payment_process_validator` — is *receiver-scoped*: it
asserts an unspent box carrying the deposit token in R4 landed at the wallet
address it is run against. That box lives at the **peer's** address, not ours,
so re-running the validator locally would be meaningless. The receiving peer
runs it inside the ``Payable`` exchange, and ``increase_deposit_on_peer`` only
returns True once the peer has accepted + validated the deposit server-side.

Instead of pretending to verify locally, once the payment settles we read back
**this node's balance registered on that peer** — the same peer-row value
shown by ``nodo peers`` — so the user can confirm the peer credited the deposit.
``increase_deposit_on_peer`` already updates that local peer row (via
``add_balance_to_peer``) after the ``Payable`` exchange, so the read reflects the
post-payment state.

SAFETY: broadcasting moves real ERG. With no funded wallet or no reachable peer
the command stops cleanly at the balance / peer-contract guard below without
sending anything.
"""

from typing import List, Optional, Tuple

from src.utils.config import ConfigManager
from src.utils.ergo_units import erg_to_nanoerg
from src.payment_system.contracts.ergo.rate import nanoerg_to_mu
from src.utils.monetary import format_mu

# Ledger id used for the peer contract-rate lookup, matching src.commands.peers.
ERGO_LEDGER = "ergo"


def _erg_to_mu(amount_erg) -> int:
    """Convert a decimal ERG amount to the node's unit of account.

    This command's amount stays in ERG whatever `ui.DISPLAY_UNIT` says, because what it
    moves is an on-chain ERG transfer: the ledger denominates it, not the operator's
    presentation preference. The MU figure is what the peer will credit, at the rate it
    advertises.
    """
    return nanoerg_to_mu(erg_to_nanoerg(amount_erg))


def _read_peer_balance(
    peer_id: str, contract_hash: str
) -> Optional[Tuple[int, Optional[int], float, Optional[str]]]:
    """Read this node's locally-recorded balance registered on ``peer_id``.

    Reuses the exact peer-row pattern from :mod:`src.commands.peers`: the peer's
    stored ``balance_mu`` / ``balance_last_update`` plus ``get_peer_contract_rate``.
    Returns ``(balance_mu, mu_per_unit, balance_last_update)``
    or ``None`` when the peer row is missing.
    """
    from src.database.sql_connection import SQLConnection

    sq = SQLConnection()
    peer = sq.get_peer_by_id(peer_id=peer_id)
    if not peer:
        return None
    balance_mu = int(peer.get("balance_mu") or 0)
    mu_per_unit = sq.get_peer_contract_rate(
        peer_id=peer_id, contract_hash=contract_hash, ledger_hash=ERGO_LEDGER
    )
    return balance_mu, mu_per_unit, peer.get("balance_last_update")


def pay(peer_id: str, amount_erg: str) -> bool:
    """Pay ``amount_erg`` ERG to ``peer_id`` via the single-wallet flow.

    Success means the tx was submitted and the receiving peer accepted +
    validated the deposit server-side; afterwards this node's balance
    registered on the peer is read back and printed so the user can confirm the
    deposit was credited.
    """
    # Deferred imports: keep the JVM / gRPC / payment graph off the fast path so
    # `nodo help`, completion, etc. never pay for it.
    from src.payment_system.contracts.ergo.interface import (
        CONTRACT_HASH,
        check_sender_balance,
    )
    from src.payment_system.payment_process import increase_deposit_on_peer
    from src.database.access_functions.ledgers import get_peer_contract_instances

    try:
        amount_mu = _erg_to_mu(amount_erg)
    except (ValueError, TypeError) as exc:
        print(f"Invalid amount '{amount_erg}': {exc}", flush=True)
        return False
    if amount_mu <= 0:
        print(f"Amount must be positive, got {amount_erg} ERG.", flush=True)
        return False

    print(
        f"Paying {amount_erg} ERG to peer {peer_id} ...",
        flush=True,
    )

    # Guard 1 — funded wallet. Clean stop at the no-funds boundary; nothing sent.
    if not check_sender_balance(amount_mu):
        print(
            "STOP: wallet balance is insufficient for this payment "
            "(no funded wallet configured?). Nothing was broadcast.",
            flush=True,
        )
        return False

    # Guard 2 — the peer advertises an Ergo payment contract. Clean stop at the
    # no-peer boundary; nothing sent. The stored row only reflects what the peer
    # advertised at handshake time, so before giving up we re-ask it: a peer that
    # had no payment contract back then may well advertise one now.
    scripts: List[Tuple[bytes, object]] = list(
        get_peer_contract_instances(CONTRACT_HASH, peer_id)
    )
    if not scripts:
        from src.manager.manager import refresh_peer_instance

        print(
            f"No Ergo payment contract known for peer {peer_id}; asking it again ...",
            flush=True,
        )
        if refresh_peer_instance(peer_id=peer_id):
            scripts = list(get_peer_contract_instances(CONTRACT_HASH, peer_id))
    if not scripts:
        print(
            f"STOP: peer {peer_id} has no known Ergo payment contract instance "
            "(it does not advertise one). Nothing was broadcast.",
            flush=True,
        )
        return False

    # Snapshot the pre-payment balance so we can show the credited delta.
    before = _read_peer_balance(peer_id, CONTRACT_HASH)

    # Pay via the existing single-wallet flow. On success the receiving peer has
    # accepted + validated the deposit with payment_process_validator (see module
    # docstring); process_payment emits its SigmaSpace URL to the node log.
    def print_transaction_url(transaction_url: str) -> None:
        print(f"Transaction URL: {transaction_url}", flush=True)

    paid = increase_deposit_on_peer(
        peer_id=peer_id,
        amount=amount_mu,
        on_transaction_url=print_transaction_url,
    )
    if not paid:
        print(
            f"FAIL: payment to peer {peer_id} did not complete or was not "
            "accepted by the peer (see node log for the transaction URL / failure reason).",
            flush=True,
        )
        return False

    print(
        f"PAID: peer {peer_id} accepted and validated the {amount_erg} ERG "
        "deposit server-side (transaction URL emitted to the node log).",
        flush=True,
    )

    # Read back this node's balance registered on the peer to confirm the
    # deposit was credited. increase_deposit_on_peer already updated the local
    # peer row (add_balance_to_peer) after the Payable exchange, so this is the
    # post-payment, locally-recorded balance.
    after = _read_peer_balance(peer_id, CONTRACT_HASH)
    if after is None:
        print(
            f"(Payment succeeded, but no local peer row was found for {peer_id} "
            "to read the balance back from.)",
            flush=True,
        )
        return True

    balance_mu, _rate, balance_last_update = after
    print(
        f"Peer {peer_id} now credits you: {format_mu(balance_mu)}, last update "
        f"{balance_last_update or 'None'}.",
        flush=True,
    )
    if before is not None:
        print(
            f"  (+{format_mu(balance_mu - before[0])} since before this payment)",
            flush=True,
        )
    return True
