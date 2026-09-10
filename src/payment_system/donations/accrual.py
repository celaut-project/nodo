"""A share of every incoming payment, owed in the asset that payment arrived in.

Why accrue at all instead of donating per payment: a 2 % cut of a small payment is
routinely below the chain's minimum payable output, and the transaction fee would eat
it whole. The counter is a rounding buffer measured in minutes or hours, not a
deferral -- :mod:`.split` pays out as soon as a transaction is worth making.

The debt is stored in the asset's smallest native unit, never in MU. A debt is
incurred at the rate of the moment it was incurred; keeping it in MU would let a later
change to that asset's rate retroactively reinterpret money this node already owes.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.payment_system.donations import config
from src.utils.logger import LOGGER

# An empty pay list with a non-zero percentage is a misconfiguration worth saying out
# loud, but this runs once per incoming payment -- so it is said once per process, and
# again by the startup validation, rather than on every payment.
_warned_about_empty_pay_list = set()


def accrue(
    *,
    amount_mu: int,
    ledger: Optional[str],
    contract_hash: Optional[str],
    asset: str,
) -> Optional[Decimal]:
    """Accrue this node's donation on one credited payment. Returns the new debt.

    Called where an incoming payment has been proved and credited to the client, and
    it must never raise there: the money has arrived and the client has been credited,
    so a failure to write the debt is a bookkeeping loss, not a reason to undo either.

    ``None`` means nothing was accrued -- a zero percentage, an empty pay list, or a
    payment method that settles on no chain (the simulated contract has no native unit
    for a debt to be denominated in, and a share of a payment nobody made is not owed
    to anybody).
    """
    try:
        if not ledger or not contract_hash:
            return None

        share = config.percentage(ledger, asset)
        if share <= 0:
            return None

        if not config.pay_wallets(ledger):
            if ledger not in _warned_about_empty_pay_list:
                _warned_about_empty_pay_list.add(ledger)
                LOGGER(
                    f"ledgers.{ledger}.payments.DONATION_PERCENTAGE is {share} but "
                    f"DONATION_WALLETS is empty, so nothing is being accrued and nothing "
                    "will be donated. Add a wallet, or set the percentage to 0."
                )
            return None

        # Imported here, not at module scope: this module is reached from the payment
        # path, and the database and payment-envs stacks both pull in heavy optional
        # dependencies. The arithmetic above must stay importable without them.
        from src.database.sql_connection import SQLConnection
        from src.payment_system.contracts import envs

        # By *method*, not by contract: one Ergo contract is paid in ERG and in every
        # token at the same address, each at its own rate, so the wrong converter would
        # denominate the debt in the wrong money.
        method = envs.resolve_method(ledger, contract_hash, asset)
        to_native = getattr(method, "mu_to_native", None) if method else None
        if to_native is None:
            return None

        if method.asset != asset:
            # The payer named no asset; this is which one it turned out to be. The share
            # is re-read against it so a per-asset DONATION_PERCENTAGE also applies to a
            # payment that left the symbol off the wire.
            asset = method.asset
            share = config.percentage(ledger, asset)
            if share <= 0:
                return None

        owed = to_native(int(amount_mu)) * share
        if owed <= 0:
            return None

        new_total = SQLConnection().accrue_donation(
            ledger=ledger,
            contract_hash=contract_hash,
            token_id=asset,
            amount_native=owed,
        )
        if new_total is not None:
            LOGGER(
                f"Donation accrued on {ledger}/{asset or 'native'}: +{owed} "
                f"(now {new_total}, in the asset's smallest unit)."
            )
        return new_total
    except Exception as e:
        # Includes a payment stack that will not import (no JVM): the debt is lost,
        # the client's credit is not.
        LOGGER(f"Could not accrue the donation for a payment on {ledger}: {e}")
        return None
