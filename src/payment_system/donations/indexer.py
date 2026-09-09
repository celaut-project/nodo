"""Index donations off every chain this node can read. Runs on the periodic tick.

Three properties this has to keep, and they are the reason it is shaped this way:

* **Local, never announced.** Every node reads the chain itself. A figure a peer sent
  us about its own generosity would be self-declared, and therefore forgeable.
* **Never on the routing path.** The balancer reads aggregates out of SQLite and does
  no network I/O at all (:mod:`.credit`). This is what fills those rows in.
* **Never raises into a caller.** An unreachable explorer is *undetermined*, not a
  verdict of "this peer donated nothing" -- the cached rows stand and the cursor stays
  where it was, so the next refresh re-reads what this one could not.
"""
from __future__ import annotations

from time import monotonic
from typing import Dict, List, Optional

from src.database.sql_connection import SQLConnection
from src.payment_system.donations import config
from src.utils.logger import LOGGER


def _scanners() -> Dict[str, object]:
    from src.payment_system.contracts import envs

    try:
        return envs.donation_scanners()
    except Exception as e:
        LOGGER(f"No donation scanner is available: {e}")
        return {}


def _attribute(sql: SQLConnection, scanner, donor_address: str) -> str:
    """Resolve a donor address to a peer id, or ``""`` when it maps to nobody yet.

    An unmapped donor is still stored: the transaction happened, and the peer may be
    introduced to this node later -- at which point
    :meth:`SQLConnection.attribute_donations` credits its whole history at once,
    without re-reading the chain.
    """
    for instance_value in scanner.instance_values_for(donor_address):
        peer_id = sql.peer_by_contract_instance(instance_value)
        if peer_id:
            return peer_id
    return ""


def refresh_ledger(ledger: str, scanner) -> int:
    """Index one ledger's counted addresses. Returns how many new donations landed."""
    counted = config.credit_wallets(ledger)
    if not counted:
        # Nothing is recognised, so nothing is a donation as far as this node is
        # concerned. Not an error: an operator may deliberately count nobody.
        return 0

    sql = SQLConnection()
    min_confirmations = config.min_confirmations(ledger)
    tip = scanner.chain_height()
    recorded = 0

    for wallet in counted:
        address = wallet.address
        cursor = sql.donation_scan_cursor(ledger, address)
        try:
            donations, truncated = scanner.scan_address(
                address,
                from_height=cursor,
                min_confirmations=min_confirmations,
            )
        except Exception as e:
            # Includes the explorer being unreachable. Cached rows stand.
            LOGGER(f"Could not scan {address} for donations on {ledger}: {e}")
            continue

        for donation in donations:
            peer_id = _attribute(sql, scanner, donation.from_address)
            if sql.record_donation(
                ledger=ledger,
                tx_id=donation.tx_id,
                to_address=donation.to_address,
                from_address=donation.from_address,
                token_id=donation.token_id,
                amount_native=str(donation.amount_native),
                tx_height=donation.tx_height,
                peer_id=peer_id or None,
            ):
                recorded += 1
            if peer_id:
                sql.attribute_donations(ledger, donation.from_address, peer_id)

        if truncated:
            # Said out loud by the scanner. The cursor stays put: advancing it would
            # read as "fully indexed" and permanently skip what the cap cut off.
            continue
        if tip is not None:
            # Stop short of the tip by the confirmation window, so the rows that are
            # still reorg-able are re-read on the next refresh. The cursor never moves
            # backwards, and the donations table's UNIQUE key makes a re-read a no-op.
            sql.set_donation_scan_cursor(ledger, address, max(0, tip - min_confirmations))

    return recorded


def refresh() -> int:
    """Index every ledger that can be read. Never raises."""
    total = 0
    for ledger, scanner in _scanners().items():
        try:
            total += refresh_ledger(ledger, scanner)
        except Exception as e:
            LOGGER(f"Donation indexing failed for {ledger}: {e}")
    if total:
        LOGGER(f"Indexed {total} new donation(s).")
    return total


# How often the index is refreshed. A constant rather than a setting: donations move on
# the timescale of a block at best, the credit they earn only ever breaks a tie between
# candidates, and every extra knob here is one an operator would have to reason about
# to gain nothing. An hour also keeps the explorer read well clear of the routing path.
REFRESH_INTERVAL_SECONDS = 3600
_last_refresh: Optional[float] = None


def tick() -> None:
    """Refresh the index if it is due. Self-gating, and never raises.

    Called from the manager's short-interval loop, the same way the energy and DDNS
    ticks are: that loop runs every few seconds, so the gate is here rather than in the
    caller's schedule.
    """
    global _last_refresh
    now = monotonic()
    if _last_refresh is not None and now - _last_refresh < REFRESH_INTERVAL_SECONDS:
        return
    _last_refresh = now
    try:
        refresh()
    except Exception as e:
        LOGGER(f"Donation index refresh failed: {e}")


def unattributed_donors() -> List[str]:
    """Donor addresses seen on-chain that map to no peer this node knows.

    Not an error state -- a peer may simply not have been introduced yet -- but useful
    to a person auditing why a donation they made is not being counted.
    """
    sql = SQLConnection()
    try:
        rows = sql._execute(
            "SELECT DISTINCT from_address FROM donations WHERE peer_id IS NULL"
        ).fetchall()
    except Exception as e:
        LOGGER(f"Could not read the unattributed donors: {e}")
        return []
    return [row['from_address'] for row in rows]
