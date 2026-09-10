"""Reading donations off Ergo. Explorer only -- no JVM, no wallet, no signing.

Deliberately separate from ``interface.py`` and as light as ``rate.py``: this runs on
the periodic tick to index what *other* nodes donated, and it must not drag the payment
stack (or a JVM) in behind it. Nothing here spends anything.

The signal never travels over the protocol. A peer telling us what it donated would be
self-declared and therefore forgeable, so every node reads the chain itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import requests

from src.payment_system.contracts.ergo.ergo_tree import p2pk_proposition_bytes_from_pk
from src.reputation_system.contracts.ergo.utils import explorer_api_url
from src.utils.ergo_units import p2pk_public_key_from_address
from src.utils.logger import LOGGER

LEDGER = "ergo"
NATIVE_ASSET = "ERG"

# Ergo's target block time. In code, not config: it is a property of the chain, and the
# only thing an operator could do with it is describe a different Ergo. Donation age is
# measured in seconds so that a block on a chain with 5x the block time does not weigh
# a donation 5x differently.
SECONDS_PER_BLOCK = 120

_PAGE_SIZE = 50
# How many pages one address may cost per refresh. A cap is needed -- an address with a
# long history would otherwise re-read it all on a first scan -- and a truncated scan
# must never advance the cursor, or it would silently skip what it did not read.
_MAX_PAGES = 20
_TIMEOUT = 30


@dataclass(frozen=True)
class ObservedDonation:
    """One donation, as it was read off the chain."""

    tx_id: str
    to_address: str
    from_address: str
    token_id: str
    amount_native: int
    tx_height: int


class ExplorerUnavailable(Exception):
    """The explorer could not be read. Undetermined, never a verdict of "no donations".

    Mirrors ``reputation_system.proof_validation.ProofLookupUnavailable``: an
    unreachable explorer means this refresh learned nothing, so the cached rows stand
    and the cursor does not move.
    """


def _get(url: str, params: Optional[dict] = None) -> dict:
    try:
        response = requests.get(url, params=params or {}, timeout=_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise ExplorerUnavailable(f"{url}: {exc}") from exc
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        raise ExplorerUnavailable(f"{url}: HTTP {response.status_code}")
    try:
        return response.json() or {}
    except ValueError as exc:
        raise ExplorerUnavailable(f"{url}: malformed JSON") from exc


def chain_height() -> Optional[int]:
    """The tip, as the explorer reports it. ``None`` when it will not say.

    Stored as each address' scan cursor, which is what lets the credit computation
    measure a donation's age without any network I/O of its own -- a routing decision
    must never wait on an explorer.
    """
    try:
        state = _get(f"{explorer_api_url().rstrip('/')}/api/v1/networkState")
    except ExplorerUnavailable as exc:
        LOGGER(f"Could not read the Ergo chain height: {exc}")
        return None
    height = state.get("height")
    try:
        return int(height)
    except (TypeError, ValueError):
        return None


def _sole_input_address(tx: dict) -> Optional[str]:
    """The one address every input of ``tx`` belongs to, or ``None``.

    A transaction spending boxes at several addresses has no single donor, and there is
    no honest way to pick one -- so it is skipped rather than guessed at. A node paying
    from its one wallet, which is the case this is for, always has a sole input address.
    """
    addresses = {
        str(box.get("address") or "")
        for box in tx.get("inputs") or []
        if box.get("address")
    }
    if len(addresses) != 1:
        return None
    return addresses.pop()


def _native_amount_to(tx: dict, address: str) -> int:
    """Total nanoERG this transaction paid to ``address``.

    Only the outputs paying that address: a change output going back to the donor, and
    anything paying somebody else, are not donations to us.

    Native ERG only. A box can also carry EIP-4 tokens, and one paying a counted
    address in a token is a real donation -- but there is no rate configured to value
    a token in MU yet (that arrives with token payments), and crediting it at ERG's
    rate would be a made-up number. Until then it is not counted rather than
    mis-counted.
    """
    total = 0
    for box in tx.get("outputs") or []:
        if str(box.get("address") or "") != address:
            continue
        try:
            total += int(box.get("value") or 0)
        except (TypeError, ValueError):
            continue
    return total


def scan_address(
    address: str,
    *,
    from_height: int,
    min_confirmations: int,
) -> Tuple[List[ObservedDonation], bool]:
    """Donations paid to ``address`` above ``from_height``, and whether the cap cut in.

    Newest first, stopping at the cursor: the explorer's address endpoint is ordered by
    inclusion height descending, so a scan of a chain this node has already indexed
    reads one page and stops.

    Raises :class:`ExplorerUnavailable` rather than returning nothing, so the caller
    can tell "no new donations" from "could not look".
    """
    api = explorer_api_url().rstrip("/")
    found: List[ObservedDonation] = []
    for page in range(_MAX_PAGES):
        payload = _get(
            f"{api}/api/v1/addresses/{address}/transactions",
            {"offset": page * _PAGE_SIZE, "limit": _PAGE_SIZE},
        )
        items = payload.get("items") or []
        if not items:
            return found, False

        for tx in items:
            try:
                height = int(tx.get("inclusionHeight") or 0)
                confirmations = int(tx.get("numConfirmations") or 0)
            except (TypeError, ValueError):
                continue
            if height <= from_height:
                # Everything from here down has been indexed already.
                return found, False
            if confirmations < min_confirmations:
                # Not final enough to count, and never read from the mempool. It stays
                # unindexed, and the cursor does not pass it, so a later refresh sees
                # it again once it has aged.
                continue

            donor = _sole_input_address(tx)
            if not donor:
                continue
            if donor == address:
                # An address paying itself is not a donation to anybody.
                continue
            amount = _native_amount_to(tx, address)
            if amount <= 0:
                continue
            found.append(ObservedDonation(
                tx_id=str(tx.get("id") or ""),
                to_address=address,
                from_address=donor,
                token_id=NATIVE_ASSET,
                amount_native=amount,
                tx_height=height,
            ))

        if len(items) < _PAGE_SIZE:
            return found, False

    LOGGER(
        f"Reached the {_MAX_PAGES * _PAGE_SIZE}-transaction cap while scanning {address} "
        "for donations; this scan is incomplete and its cursor is not advanced."
    )
    return found, True


def native_to_mu(amount_native: int, asset: str = NATIVE_ASSET) -> int:
    """A donation's amount, in this node's MU, at this ledger's own rate.

    Aggregating credit across chains needs one comparable number per donation, and MU
    is what the node already compares money in. The rate is this ledger's, which is the
    same one its payments convert through.

    An asset this node has no rate for contributes 0 rather than a guess: a token
    valued at ERG's rate would be a made-up figure that silently moves routing.
    """
    if asset != NATIVE_ASSET:
        return 0
    from src.payment_system.contracts.ergo import rate

    return rate.nanoerg_to_mu(int(amount_native))


def instance_values_for(address: str) -> Iterable[str]:
    """How ``address`` would appear as a stored payment contract instance.

    A peer announces its payment address as the raw P2PK propositionBytes, stored as
    hex (``sql_connection.add_contract``), so matching a donor address against what
    peers announced means deriving the same bytes -- purely, with no JVM.

    This is how a donor is identified off a chain where a peer's identity key is not
    its wallet key: the peer announced the address it wants to be *paid* at, and that
    binding is economic rather than cryptographic. Announcing an address you do not
    control means giving your revenue away, and claiming another peer's address to
    steal its donation credit costs you 100 % of your income in that currency.
    """
    key = p2pk_public_key_from_address(address)
    if key is None:
        return ()
    return (p2pk_proposition_bytes_from_pk(key).hex(),)
