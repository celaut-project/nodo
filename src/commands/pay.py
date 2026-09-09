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

from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from src.utils.config import ConfigManager
from src.utils.monetary import format_mu


def _method_for(ledger: Optional[str], asset: Optional[str],
                payment_method: Optional[str]):
    """The payment method to pay through, and the reason when there is none.

    A payment method is ``(ledger, contract, asset)``, so the ledger alone does not
    name one: an Ergo P2PK contract is paid in ERG and in every configured token, at
    different rates. ``--ledger ergo`` on a node that accepts SigUSD is exactly as
    ambiguous as no flag at all on a node that also accepts Bitcoin.

    Three ways to say the same thing, because they are the same thing:
    ``--payment-method ergo:sigusd``, ``--ledger ergo --asset sigusd``, and -- when
    only one method is offered -- nothing.

    An asset is matched by its display symbol, its unit name or its id. The id is what
    it *is* (a name is not an identity, anyone can mint a token called SigUSD), but an
    operator typing a command has the symbol in front of them, and the two cannot
    collide: an id is 64 hex characters.
    """
    from src.payment_system.contracts.registry import attribute, methods

    if payment_method:
        if ":" in payment_method:
            named_ledger, named_asset = payment_method.split(":", 1)
        else:
            # `--payment-method ergo` is a ledger, and reads better as one than as an
            # error about a missing colon.
            named_ledger, named_asset = payment_method, ""
        ledger = ledger or named_ledger.strip() or None
        asset = asset or named_asset.strip() or None

    offered = {
        key: method for key, method in methods().items()
        if not attribute(method, "is_demo")
    }
    if not offered:
        return None, (
            "this node offers no payment system, so it cannot pay anybody. Check "
            "`ledgers:` and that the ledger's runtime is reachable."
        )

    def names(method) -> tuple:
        """Every way an operator may name this method's asset."""
        return tuple(str(name).lower() for name in (
            method.asset, method.symbol, method.unit_name,
        ) if name)

    candidates = list(offered.values())
    if ledger:
        candidates = [m for m in candidates if m.LEDGER == ledger]
        if not candidates:
            return None, (
                f"this node does not offer {ledger!r}. It offers: "
                f"{', '.join(sorted({m.LEDGER for m in offered.values()}))}."
            )
    if asset:
        wanted = asset.strip().lower()
        matched = [m for m in candidates if wanted in names(m)]
        if not matched:
            return None, (
                f"this node does not accept {asset!r}"
                + (f" on {ledger}" if ledger else "")
                + f". It accepts: {', '.join(_offered_names(candidates))}."
            )
        candidates = matched
    if len(candidates) > 1:
        return None, (
            "this node offers more than one payment method "
            f"({', '.join(_offered_names(candidates))}), so the amount is ambiguous. "
            "Say which with --payment-method <ledger>:<asset>."
        )
    return candidates[0], None


def _offered_names(methods_) -> List[str]:
    """How the offered methods are named back to an operator who has to pick one."""
    return sorted(f"{m.LEDGER}:{_asset_name(m)}" for m in methods_)


def _asset_name(method) -> str:
    """The asset as a person reads it: its symbol when it has one, else its id."""
    return str(method.symbol or method.asset)


def _amount_to_mu(contract, amount: str) -> int:
    """A whole-unit amount of this method's asset, as this node's MU.

    The amount stays in the asset's own unit whatever `ui.DISPLAY_UNIT` says -- ERG for
    Ergo's native method, BTC for Bitcoin's, whole SigUSD for that token's -- because
    what moves is an on-chain transfer and the asset denominates it, not the operator's
    presentation preference.

    Converted through ``mu_per_unit()``, which is the same figure peers are told as
    ``ContractRate``, so what the operator types and what the peer credits are related
    by one number rather than by two conversions that can disagree.
    """
    try:
        typed = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"not a number: {exc}") from exc
    if typed < 0:
        raise ValueError("must not be negative")
    in_mu = typed * Decimal(contract.mu_per_unit())
    if in_mu != in_mu.to_integral_value():
        raise ValueError(
            f"{amount} {_asset_name(contract)} is not a whole number of MU, and MU is "
            "the unit of account -- there is nothing smaller to express"
        )
    return int(in_mu)


def _read_peer_balance(
    peer_id: str, contract_hash: str, ledger: str
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
        peer_id=peer_id, contract_hash=contract_hash, ledger_hash=ledger
    )
    return balance_mu, mu_per_unit, peer.get("balance_last_update")


def pay(peer_id: str, amount: str, ledger: Optional[str] = None,
        asset: Optional[str] = None, payment_method: Optional[str] = None) -> bool:
    """Pay ``amount`` whole units of one payment method's asset to ``peer_id``.

    The method is named by ``--payment-method <ledger>:<asset>``, or by
    ``--ledger``/``--asset``, or not at all when this node offers exactly one. It is
    what the amount is read in *and* what the payment settles through: without the
    second half the flag would only pick the rate, and a payment named in SigUSD could
    settle in ERG.

    Success means the tx was submitted and the receiving peer accepted +
    validated the deposit server-side; afterwards this node's balance
    registered on the peer is read back and printed so the user can confirm the
    deposit was credited.
    """
    # Deferred imports: keep the JVM / gRPC / payment graph off the fast path so
    # `nodo help`, completion, etc. never pay for it.
    from src.payment_system.payment_process import (
        deposit_refusal_reason,
        increase_deposit_on_peer,
    )
    from src.database.access_functions.ledgers import get_peer_contract_instances

    contract, refusal = _method_for(ledger, asset, payment_method)
    if contract is None:
        print(f"STOP: {refusal}", flush=True)
        return False
    unit = _asset_name(contract) or contract.LEDGER
    contract_hash = contract.CONTRACT_HASH

    try:
        amount_mu = _amount_to_mu(contract, amount)
    except (ValueError, TypeError) as exc:
        print(f"Invalid amount '{amount}': {exc}", flush=True)
        return False
    if amount_mu <= 0:
        print(f"Amount must be positive, got {amount} {unit}.", flush=True)
        return False

    print(f"Paying {amount} {unit} to peer {peer_id} ...", flush=True)

    # Guard 0 — the amount can be settled at all. Clean stop before the wallet is
    # touched: below the ledger's minimum output no transaction can be built, and
    # an operator's figure is refused rather than quietly raised (see
    # `increase_deposit_on_peer`). Also catches a peer we share no payment system
    # with, which is the same kind of "nothing was broadcast" answer.
    refusal = deposit_refusal_reason(peer_id, amount_mu)
    if refusal:
        print(f"STOP: {refusal}.", flush=True)
        return False

    # Guard 1 — funded wallet. Clean stop at the no-funds boundary; nothing sent.
    if not contract.check_sender_balance(amount_mu):
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
    # This method's instances, not the contract's: on Ergo every asset of a contract
    # shares an address, so the contract's rows would say "yes, payable" for an asset
    # the peer never advertised.
    scripts: List[Tuple[bytes, object, str]] = list(
        get_peer_contract_instances(contract_hash, peer_id, contract.asset)
    )
    if not scripts:
        from src.manager.manager import refresh_peer_instance

        print(
            f"No {contract.LEDGER} {unit} payment method known for peer {peer_id}; "
            "asking it again ...",
            flush=True,
        )
        if refresh_peer_instance(peer_id=peer_id):
            scripts = list(
                get_peer_contract_instances(contract_hash, peer_id, contract.asset)
            )
    if not scripts:
        print(
            f"STOP: peer {peer_id} does not accept {unit} on {contract.LEDGER} (it "
            "advertises no such payment method). Nothing was broadcast.",
            flush=True,
        )
        return False

    # Snapshot the pre-payment balance so we can show the credited delta.
    before = _read_peer_balance(peer_id, contract_hash, contract.LEDGER)

    # Pay via the existing single-wallet flow. On success the receiving peer has
    # accepted + validated the deposit with payment_process_validator (see module
    # docstring); process_payment emits its SigmaSpace URL to the node log.
    def print_transaction_url(transaction_url: str) -> None:
        print(f"Transaction URL: {transaction_url}", flush=True)

    paid = increase_deposit_on_peer(
        peer_id=peer_id,
        amount=amount_mu,
        on_transaction_url=print_transaction_url,
        # The method the operator named, so the payment settles in the asset the amount
        # was read in rather than in whichever one happens to be funded first.
        method=contract.key,
    )
    if not paid:
        print(
            f"FAIL: payment to peer {peer_id} did not complete or was not "
            "accepted by the peer (see node log for the transaction URL / failure reason).",
            flush=True,
        )
        return False

    print(
        f"PAID: peer {peer_id} accepted and validated the {amount} {unit} "
        "deposit server-side (transaction URL emitted to the node log).",
        flush=True,
    )

    # Read back this node's balance registered on the peer to confirm the
    # deposit was credited. increase_deposit_on_peer already updated the local
    # peer row (add_balance_to_peer) after the Payable exchange, so this is the
    # post-payment, locally-recorded balance.
    after = _read_peer_balance(peer_id, contract_hash, contract.LEDGER)
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
