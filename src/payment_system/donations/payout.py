"""Paying an accrued donation debt. Shared by every payment system that can pay one.

Ergo and Bitcoin want the same eight steps in the same order, and only the units and the
send call differ. Copying them would put the rule that matters -- *decrement the debt
only after the transaction is on the wire, in the same transaction as the rows that
record it* -- in two places, on the one path where drifting means paying the same debt
twice or losing the record of one that went out.

What a contract supplies is its own money: its floors in base units, how to render an
amount, and how to send. What this owns is the order and the bookkeeping.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple

from src.payment_system.donations import config as donation_config
from src.payment_system.donations.split import plan_payout
from src.utils.logger import LOGGER

#: ``(address, amount in base units)`` pairs to pay, and the fee to pay them with.
Sender = Callable[[List[Tuple[str, int]], int], str]
#: A base-unit integer as a person should read it, with its unit.
Render = Callable[[int], str]


def pay_accrued(
    *,
    ledger: str,
    contract_hash: str,
    asset: str,
    fee: int,
    minimum_output: int,
    min_transfer: int,
    send: Sender,
    render: Render,
    #: Base units -> MU, for the ledger-neutral column on the payment rows.
    to_mu: Callable[[int], int],
    valid_address: Callable[[str], bool],
    simulate: bool,
    available: Optional[Callable[[int], bool]] = None,
    lock=None,
) -> bool:
    """Pay what is owed on one payment method, if a transaction is worth making.

    Returns whether a transaction went out. Never raises: this runs on the periodic
    tick, and a donation that could not be paid this time is still owed next time --
    which is the whole reason the debt is a row and not a decision made per payment.

    ``available(total)`` is the contract's own "can the wallet cover this" check, asked
    before broadcasting so the log names the reason rather than whatever the chain
    raises. ``lock`` is the contract's payment lock, so a donation cannot race a deposit
    for the same inputs.
    """
    from src.database.sql_connection import SQLConnection

    try:
        sql = SQLConnection()
        owed = sql.donation_owed(ledger, contract_hash, asset)
        if owed <= 0:
            return False

        wallets = donation_config.pay_wallets(ledger)
        # Refused at startup, so an address that does not parse here means the config
        # changed underneath a running node. The bad entry is skipped rather than the
        # whole payout -- one unparseable wallet would otherwise block every donation
        # for ever -- but it keeps its weight, so its share stays accrued instead of
        # being paid to the wallets that happen to parse. Corrected, it is paid what it
        # was always owed.
        unpayable = {w.address for w in wallets if not valid_address(w.address)}
        for address in sorted(unpayable):
            LOGGER(
                f"Skipping donation wallet {address!r} on {ledger}: not a valid address "
                "for this chain. Its share stays accrued."
            )
        if len(unpayable) == len(wallets):
            LOGGER(
                f"{render(int(owed))} is owed in donations on {ledger} but no valid "
                "wallet is configured to receive it; it stays accrued."
            )
            return False

        plan = plan_payout(
            owed,
            wallets,
            unpayable=unpayable,
            min_transfer_native=min_transfer,
            # The chain's own floors, in its own units. Passing them in native rather
            # than reading `settlement_floors_mu()` back is what keeps the comparison
            # honest: the debt is native, and a floor in MU would be wrong by exactly
            # this asset's rate -- invisible at a rate of 1, silently wrong after.
            min_payable_native=minimum_output,
            fee_native=fee,
        )
        if plan is None:
            LOGGER(
                f"Donation debt of {render(int(owed))} on {ledger} is not yet worth a "
                f"transaction (min transfer {render(min_transfer)}, fee {render(fee)}, "
                f"minimum output {render(minimum_output)}); it stays accrued."
            )
            return False

        rendered = ", ".join(
            f"{render(amount)} -> {address}" for address, amount in plan.outputs
        )
        if simulate:
            LOGGER(
                f"SIMULATE_PAYMENTS is on: would donate {rendered} on {ledger} (fee "
                f"{render(plan.fee_native)}). Nothing broadcast, and the debt stays owed."
            )
            return False

        if available is not None and not available(plan.total_native):
            # The debt came out of money that arrived, but peer deposits come out of the
            # same wallet, so the funds can be gone by the time the tick fires.
            LOGGER(
                f"Not paying the donation on {ledger} yet: it needs "
                f"{render(plan.total_native)} and the wallet cannot cover it. The debt "
                "stays accrued."
            )
            return False

        if lock is not None:
            with lock:
                tx_id = send(plan.outputs, plan.fee_native)
        else:
            tx_id = send(plan.outputs, plan.fee_native)
        tx_id = str(tx_id) if tx_id else ""
        LOGGER(f"Donation tx on {ledger} -> {tx_id}: {rendered}")

        if not sql.settle_donation(
            ledger=ledger,
            contract_hash=contract_hash,
            token_id=asset,
            paid_native=plan.total_native,
            records=records_for(tx_id, plan.outputs, to_mu),
        ):
            # The transaction is on the chain and the debt is not discharged, so the
            # next tick will pay it again. Nothing here can undo it, and the debt row is
            # the only thing that could have stopped the repeat -- so this is said as
            # loudly as a log line can say it.
            LOGGER(
                f"[ERROR] Donation tx {tx_id} on {ledger} was broadcast but the debt "
                f"could not be decremented: {render(plan.total_native)} may be donated "
                "again on the next tick. Check the donation_accrual row against "
                "`nodo tx_history` before the next payment manager iteration."
            )
        if plan.withheld_native > 0:
            LOGGER(
                f"{plan.withheld_native} base units stay accrued on {ledger}: a share "
                "below the chain's minimum output, plus the sub-unit remainder."
            )
        return True
    except Exception as e:
        LOGGER(f"Exception while paying accrued donations on {ledger} -> {e}")
        return False


def records_for(tx_id: str, outputs: Sequence[Tuple[str, int]],
                to_mu: Callable[[int], int]) -> List[dict]:
    """The payment rows one donation transaction produces.

    In MU for the ledger-neutral column, with the destination on each. ``peer_id`` is
    left unset on purpose: a donation goes to a wallet, and the wallet of a developer is
    not a peer this node routes work to. What makes the row a donation is ``purpose``.
    """
    return [
        {"tx_id": tx_id, "address": address, "amount_mu": to_mu(amount)}
        for address, amount in outputs
    ]
