"""Translate amounts between two nodes' private MU scales.

MU is deliberately local to a node.  A payment contract's ``mu_per_unit`` is
therefore the bridge: it says how many of *that node's* MU one ledger unit is
worth.  Amounts passed to a peer must use the peer's rate; amounts charged to a
local client must use ours.

Rounding always goes the direction that cannot cost this node money: what we
promise or hand to a peer rounds **down**, what we owe or charge back to a
local client rounds **up**.  Refusing inexact conversions instead would abort
delegation for most rate pairs, and on the payment path it would abort with the
money already on-chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha3_256
from typing import TYPE_CHECKING, Iterable, List, Mapping, Optional

if TYPE_CHECKING:
    from src.database.sql_connection import SQLConnection


@dataclass(frozen=True)
class MatchingPaymentSystem:
    """The one contract/ledger pair through which two nodes can settle."""

    ledger_tag: str
    contract_hash: str
    local_mu_per_unit: int
    peer_mu_per_unit: int


def _rates_by_payment_system(
        contracts: Iterable[Mapping[str, object]],
        *,
        owner: str,
) -> dict[tuple[str, str], int]:
    """Index valid advertised rates, rejecting conflicting duplicate rows."""
    rates: dict[tuple[str, str], int] = {}
    for contract in contracts:
        ledger_tag = contract.get("ledger_tag")
        contract_hash = contract.get("contract_hash")
        raw_rate = contract.get("mu_per_unit")
        if not ledger_tag or not contract_hash:
            continue
        try:
            rate = int(raw_rate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue

        key = (str(ledger_tag), str(contract_hash))
        previous = rates.setdefault(key, rate)
        if previous != rate:
            raise ValueError(
                f"{owner} advertises conflicting MU rates for payment system "
                f"{key[0]}/{key[1][:12]}."
            )
    return rates


def _local_rates() -> dict[tuple[str, str], int]:
    """Our own rates, read from what we advertise rather than from the database.

    The ``LOCAL`` row in ``contract_instance`` is written by each ledger's
    ``init()`` through ``add_contract``'s default ``mu_per_unit=0``, and nothing
    ever writes a real rate into it -- so reading our rate from there matched
    nothing and made every peer look incompatible.  ``local_payment_methods``
    is the single source of truth: it is literally the ``ContractRate`` a peer
    receives from us, so both sides key on the same numbers.
    """
    from src.payment_system.ledgers import local_payment_methods
    from src.utils.contract_xattrs import get_contract_type
    from src.utils.utils import from_amount

    rates: dict[tuple[str, str], int] = {}
    for advertised in local_payment_methods():
        ledger = advertised.contract.ledger
        # Same derivation `add_contract` uses for the peer rows this is matched
        # against: the stable, wallet-independent contract type.
        type_bytes = get_contract_type(advertised.contract)
        if not ledger.tags or not type_bytes:
            continue
        rate = from_amount(advertised.mu_per_unit)
        if rate <= 0:
            continue
        rates[(ledger.tags[0], sha3_256(type_bytes).hexdigest())] = rate
    return rates


def matching_payment_systems(
        peer_id: str,
        connection: Optional["SQLConnection"] = None,
) -> List[MatchingPaymentSystem]:
    """Every payment system shared with ``peer_id``, in the order to try them.

    This used to be a single system and it *raised* when two nodes shared more than
    one: "payment selection is not implemented". Two nodes that both accepted ERG and
    BTC would therefore have been unable to pay each other at all -- strictly worse
    than accepting one currency each.

    There is still no preference policy, and none is needed: **funding is the
    selection**. The payer walks this list and settles through the first system whose
    wallet can cover the amount, which is the whole requirement. Ordering can become a
    policy the day there is a reason to prefer one over another.

    The order is the registry's own candidate order, not alphabetical: it is what
    `local_payment_methods` yields, so the payer's preference is declared in one place
    (``contracts/registry.py``) instead of falling out of how contract hashes happen to
    sort. Deterministic either way -- and it has to be, or which system settles a
    payment would vary between runs.

    Raises only when there is nothing shared at all, which is a real failure: no amount
    of retrying finds a currency two nodes do not both accept.
    """
    if connection is None:
        # Keep the arithmetic helpers importable on their own (including in
        # configuration/diagnostic contexts that do not load the gRPC database
        # stack).
        from src.database.sql_connection import SQLConnection
        connection = SQLConnection()
    local_rates = _local_rates()
    peer_rates = _rates_by_payment_system(
        connection.get_peer_payment_contracts(peer_id), owner=f"peer {peer_id!r}"
    )
    shared = [key for key in local_rates if key in peer_rates]
    if not shared:
        raise ValueError(
            f"no common payment system is registered for peer {peer_id!r}"
        )
    return [
        MatchingPaymentSystem(
            ledger_tag=ledger_tag,
            contract_hash=contract_hash,
            local_mu_per_unit=local_rates[(ledger_tag, contract_hash)],
            peer_mu_per_unit=peer_rates[(ledger_tag, contract_hash)],
        )
        for ledger_tag, contract_hash in shared
    ]


def matching_payment_system(
        peer_id: str,
        connection: Optional["SQLConnection"] = None,
) -> MatchingPaymentSystem:
    """The first payment system shared with ``peer_id``.

    For the callers that need *a rate* rather than a settlement: converting a balance a
    peer holds for us, a cost it just metered, a configuration we are about to quote it.
    Those produce one figure and cannot "try the next one".

    Taking the first is sound rather than arbitrary. MU is each node's own scale, so the
    ratio between two nodes' MU is a property of the pair, not of the route: with both
    sides configured coherently every shared system gives the same answer, and where
    they disagree it is because one operator's rates are inconsistent with each other --
    which is what the startup settle-check reports, and not something a conversion can
    paper over.

    The *payment* path must not use this. It has to convert at the rate of the contract
    that actually settled, which is why `matching_payment_systems` exists and why the
    conversion now lives inside the payer's loop.
    """
    return matching_payment_systems(peer_id, connection)[0]


def convert_mu(
        amount_mu: int,
        *,
        from_mu_per_unit: int,
        to_mu_per_unit: int,
        round_up: bool = False,
) -> int:
    """Convert MU through the common ledger unit, to a whole MU of the target scale.

    The protocol has no fractional MU, so a conversion that is not exact has to
    land on one side of the true value.  ``round_up`` picks which: leave it off
    for an amount handed to a peer (never promise more than the value moved),
    turn it on for an amount charged back to a local client (never charge less
    than the delegation costs us).
    """
    amount_mu = int(amount_mu)
    from_mu_per_unit = int(from_mu_per_unit)
    to_mu_per_unit = int(to_mu_per_unit)
    if amount_mu < 0:
        raise ValueError("cannot convert a negative MU amount")
    if from_mu_per_unit <= 0 or to_mu_per_unit <= 0:
        raise ValueError("MU-per-unit rates must be positive")

    numerator = amount_mu * to_mu_per_unit
    if round_up:
        return -(-numerator // from_mu_per_unit)
    return numerator // from_mu_per_unit


def peer_mu_in_local(peer_id: str, amount_mu: int, *, round_up: bool = False) -> Optional[int]:
    """A figure of the peer's, expressed in our MU, or None if nothing says what it is worth.

    The single crossing for every peer figure that enters this node against a live
    rate -- a balance we hold there, a cost the peer just metered -- so the rate is
    read at the moment of use rather than frozen into a stored number.

    ``round_up`` is the caller's, because the safe direction is not the same for
    both: understating an asset only makes this node spend less than it could, while
    understating a cost has the node pay the difference.

    None rather than zero: "no common payment system" is not "nothing there", and
    charging a client zero for runtime that was really consumed is a different
    mistake from declining to charge at all.
    """
    try:
        payment_system = matching_payment_system(peer_id)
    except ValueError:
        return None
    return convert_mu(
        int(amount_mu),
        from_mu_per_unit=payment_system.peer_mu_per_unit,
        to_mu_per_unit=payment_system.local_mu_per_unit,
        round_up=round_up,
    )


def configuration_for_peer(config, *, payment_system: MatchingPaymentSystem):
    """Copy ``config`` and express its initial balance in the peer's MU.

    A configuration arrives at a node in that node's MU.  Do not mutate it:
    the same configuration may be used to quote several peers, each with a
    different scale, or to launch locally after those quotes.
    """
    peer_config = type(config)()
    peer_config.CopyFrom(config)
    if peer_config.HasField("initial_mu"):
        peer_initial_mu = convert_mu(
            int(peer_config.initial_mu.n),
            from_mu_per_unit=payment_system.local_mu_per_unit,
            to_mu_per_unit=payment_system.peer_mu_per_unit,
        )
        peer_config.initial_mu.n = str(peer_initial_mu)
    return peer_config


def estimated_cost_for_local(estimated_cost, *, payment_system: MatchingPaymentSystem):
    """Copy a peer quote and express every monetary field in local MU."""
    local_cost = type(estimated_cost)()
    local_cost.CopyFrom(estimated_cost)
    for field in ("cost", "init_maintenance_cost", "max_maintenance_cost"):
        amount = getattr(local_cost, field)
        amount.n = str(convert_mu(
            int(amount.n or 0),
            from_mu_per_unit=payment_system.peer_mu_per_unit,
            to_mu_per_unit=payment_system.local_mu_per_unit,
            # What we will owe the peer; rounding it down would charge our own
            # client less than the delegation costs us.
            round_up=True,
        ))
    return local_cost
