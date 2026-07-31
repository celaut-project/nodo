"""Pay ERG to a specific peer via the node's single-wallet flow (user-invoked dev command).

This reuses the node's *existing* single-wallet payment machinery — no crypto,
payment, or gRPC logic is reimplemented here:

  Pay -> :func:`src.payment_system.payment_process.increase_deposit_on_peer`,
         which drives
         :func:`src.payment_system.contracts.ergo.interface.process_payment`
         (build + sign + broadcast + wait for confirmations; the tx id is
         emitted to the node log) and then the ``Payable`` gRPC round-trip.

There is deliberately **no payer-side verify** step. The authoritative check —
:func:`...ergo.interface.payment_process_validator` — is *receiver-scoped*: it
asserts an unspent box carrying the deposit token in R4 landed at the wallet
address it is run against. That box lives at the **peer's** address, not ours,
so re-running the validator locally would be meaningless. The receiving peer
runs it inside the ``Payable`` exchange, and ``increase_deposit_on_peer`` only
returns True once the peer has accepted + validated the deposit server-side.

Instead of pretending to verify locally, once the payment settles we read back
**this node's gas balance registered on that peer** — the same peer-row value
shown by ``nodo peers`` — so the user can confirm the peer credited the deposit.
``increase_deposit_on_peer`` already updates that local peer row (via
``add_gas_to_peer``) after the ``Payable`` exchange, so the read reflects the
post-payment state.

SAFETY: broadcasting moves real ERG. With no funded wallet or no reachable peer
the command stops cleanly at the balance / peer-contract guard below without
sending anything.
"""

from typing import List, Optional, Tuple

from src.utils.config import ConfigManager
from src.utils.ergo_units import erg_to_nanoerg
from src.utils.logger import ssformat

# Ledger id used for the peer gas-price lookup, matching src.commands.peers.
ERGO_LEDGER = "ergo"


def _erg_to_gas(amount_erg) -> int:
    """Convert a decimal ERG amount to the node's internal gas unit.

    Inverts ``interface.__gas_to_nanoerg`` (nanoERG = gas / GAS_PER_ERG) with the
    same ``ledgers.ergo.GAS_PER_ERG`` config value, so no conversion math is
    duplicated: gas = erg_to_nanoerg(erg) * GAS_PER_ERG.
    """
    gas_per_erg = int(ConfigManager().get("ledgers.ergo.GAS_PER_ERG"))
    return erg_to_nanoerg(amount_erg) * gas_per_erg


def _read_peer_gas(
    peer_id: str, contract_hash: str
) -> Optional[Tuple[int, Optional[int], float, Optional[str]]]:
    """Read this node's locally-recorded gas balance registered on ``peer_id``.

    Reuses the exact peer-row pattern from :mod:`src.commands.peers`: the peer's
    stored ``gas`` / ``gas_last_update`` plus ``get_peer_gas_price`` to convert
    gas -> nanoERG. Returns ``(gas, gas_price, gas_on_nanoerg, gas_last_update)``
    or ``None`` when the peer row is missing.
    """
    from src.database.sql_connection import SQLConnection

    sq = SQLConnection()
    peer = sq.get_peer_by_id(peer_id=peer_id)
    if not peer:
        return None
    gas = int(peer.get("gas") or 0)
    gas_price = sq.get_peer_gas_price(
        peer_id=peer_id, contract_hash=contract_hash, ledger_hash=ERGO_LEDGER
    )
    gas_on_nanoerg = (gas / gas_price) if gas_price else 0
    return gas, gas_price, gas_on_nanoerg, peer.get("gas_last_update")


def pay(peer_id: str, amount_erg: str) -> bool:
    """Pay ``amount_erg`` ERG to ``peer_id`` via the single-wallet flow.

    Success means the tx was submitted and the receiving peer accepted +
    validated the deposit server-side; afterwards this node's gas balance
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
        gas_amount = _erg_to_gas(amount_erg)
    except (ValueError, TypeError) as exc:
        print(f"Invalid amount '{amount_erg}': {exc}", flush=True)
        return False
    if gas_amount <= 0:
        print(f"Amount must be positive, got {amount_erg} ERG.", flush=True)
        return False

    print(
        f"Paying {amount_erg} ERG ({gas_amount} gas) to peer {peer_id} ...",
        flush=True,
    )

    # Guard 1 — funded wallet. Clean stop at the no-funds boundary; nothing sent.
    if not check_sender_balance(gas_amount):
        print(
            "STOP: wallet balance is insufficient for this payment "
            "(no funded wallet configured?). Nothing was broadcast.",
            flush=True,
        )
        return False

    # Guard 2 — the peer advertises an Ergo payment contract. Clean stop at the
    # no-peer boundary; nothing sent.
    scripts: List[Tuple[bytes, object]] = list(
        get_peer_contract_instances(CONTRACT_HASH, peer_id)
    )
    if not scripts:
        print(
            f"STOP: peer {peer_id} has no known Ergo payment contract instance. "
            "Nothing was broadcast.",
            flush=True,
        )
        return False

    # Snapshot the pre-payment balance so we can show the credited delta.
    before = _read_peer_gas(peer_id, CONTRACT_HASH)

    # Pay via the existing single-wallet flow. On success the receiving peer has
    # accepted + validated the deposit with payment_process_validator (see module
    # docstring); the tx id is emitted to the node log by process_payment.
    paid = increase_deposit_on_peer(peer_id=peer_id, amount=gas_amount)
    if not paid:
        print(
            f"FAIL: payment to peer {peer_id} did not complete or was not "
            "accepted by the peer (see node log for the tx id / failure reason).",
            flush=True,
        )
        return False

    print(
        f"PAID: peer {peer_id} accepted and validated the {amount_erg} ERG "
        "deposit server-side (tx id emitted to the node log).",
        flush=True,
    )

    # Read back this node's gas balance registered on the peer to confirm the
    # deposit was credited. increase_deposit_on_peer already updated the local
    # peer row (add_gas_to_peer) after the Payable exchange, so this is the
    # post-payment, locally-recorded balance.
    after = _read_peer_gas(peer_id, CONTRACT_HASH)
    if after is None:
        print(
            f"(Payment succeeded, but no local peer row was found for {peer_id} "
            "to read the gas balance back from.)",
            flush=True,
        )
        return True

    gas, _gas_price, gas_on_nanoerg, gas_last_update = after
    print(
        f"Peer {peer_id} now credits you: {ssformat(gas)} gas "
        f"({ssformat(gas_on_nanoerg)} nanoERG), last update "
        f"{gas_last_update or 'None'}.",
        flush=True,
    )
    if before is not None:
        print(
            f"  (+{ssformat(gas - before[0])} gas since before this payment)",
            flush=True,
        )
    return True
