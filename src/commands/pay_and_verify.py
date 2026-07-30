"""Pay ERG to a specific peer and verify the payment (user-invoked dev command).

This reuses the node's *existing* single-wallet payment machinery — no crypto or
payment logic is reimplemented here:

  Pay    -> :func:`src.payment_system.payment_process.increase_deposit_on_peer`,
            which drives
            :func:`src.payment_system.contracts.ergo.interface.process_payment`
            (build + sign + broadcast + wait for confirmations; the tx id is
            emitted to the node log) and then the ``Payable`` gRPC round-trip.
  Verify -> :func:`...ergo.interface.payment_process_validator`. This validator
            is *receiver-scoped*: it asserts an unspent box carrying the deposit
            token in R4 landed at the wallet address it is run against. It is the
            authoritative check the **receiving peer** runs inside the ``Payable``
            exchange above, so ``increase_deposit_on_peer`` only returns True when
            the peer has validated the payment with it. (A sender-side call for a
            peer-directed payment is not meaningful — the box lives at the peer's
            address, not ours — which is why we surface the peer's verification
            rather than re-running the validator locally against our own wallet.)

SAFETY: broadcasting moves real ERG. With no funded wallet or no reachable peer
the command stops cleanly at the balance / peer-contract guard below without
sending anything.
"""

from typing import List, Tuple

from src.utils.config import ConfigManager
from src.utils.ergo_units import erg_to_nanoerg


def _erg_to_gas(amount_erg) -> int:
    """Convert a decimal ERG amount to the node's internal gas unit.

    Inverts ``interface.__gas_to_nanoerg`` (nanoERG = gas / GAS_PER_ERG) with the
    same ``ledgers.ergo.GAS_PER_ERG`` config value, so no conversion math is
    duplicated: gas = erg_to_nanoerg(erg) * GAS_PER_ERG.
    """
    gas_per_erg = int(ConfigManager().get("ledgers.ergo.GAS_PER_ERG"))
    return erg_to_nanoerg(amount_erg) * gas_per_erg


def pay_and_verify(peer_id: str, amount_erg: str) -> bool:
    """Pay ``amount_erg`` ERG to ``peer_id`` and report whether it was verified."""
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

    # Pay via the existing single-wallet flow. On success the receiving peer has
    # already verified the deposit with payment_process_validator (see module
    # docstring); the tx id is emitted to the node log by process_payment.
    paid = increase_deposit_on_peer(peer_id=peer_id, amount=gas_amount)
    if paid:
        print(
            f"PAID + VERIFIED: peer {peer_id} accepted and validated the "
            f"{amount_erg} ERG deposit (tx id emitted to the node log).",
            flush=True,
        )
        return True

    print(
        f"FAIL: payment to peer {peer_id} did not complete or was not verified "
        "by the peer (see node log for the tx id / failure reason).",
        flush=True,
    )
    return False
