"""``nodo donations`` -- what this node donates, and whose donations it counts.

A non-zero donation default has to be auditable. An operator who finds that 2 % of
their earnings leaves the node must be able to read, in one place, where it goes, how
much is waiting to go, and what the node gets back for it -- otherwise the honest thing
would be to default it to zero.

Two audiences, one computation, the same shape as ``nodo reputation``: a person reads
the printed form, and the TUI reads ``--json``. The credit arithmetic lives on the
Python side (``payment_system.donations.credit``) and is reported from here rather than
re-derived by the interface, because a second implementation of a number that decides
where work is routed is a second number.

Read-only. Nothing here signs, spends or accrues anything.
"""

import json
import sys
import time
from decimal import Decimal
from typing import Dict, List, Optional


def _wallet_json(wallet, normalised: Dict[str, Decimal], other: set,
                 paid: Optional[Dict[str, Dict[str, str]]] = None) -> dict:
    """One wallet list entry, and -- for a pay list -- what has reached it.

    ``paid`` is per asset, because a credit in nanoERG says nothing about what a wallet
    has had in a token. It is the figure that makes a *weight* checkable: a wallet given
    0.1 % of this node's earnings should be able to show that something arrived, and the
    first version of this circuit could not, because a cut too small to send went back
    into a debt belonging to nobody and was shared out again on the next tick.
    """
    entry = {
        "address": wallet.address,
        "weight": str(wallet.weight),
        "normalised": str(normalised.get(wallet.address, Decimal(0))),
        "in_other_list": wallet.address in other,
    }
    if paid is not None:
        entry["paid_native"] = paid.get(wallet.address, {})
    return entry


def _coherence_warnings(ledger: str, funded: set, counted: set) -> List[str]:
    """The two mismatches an operator will actually want to review.

    Neither is an error -- both are legitimate positions -- but both are usually an
    oversight, and each has a different cost. Funding someone this node does not count
    means paying for a contribution it does not recognise, which also costs this node
    its own donation credit (§ the balancer computes ours with the same list as
    everyone's). Counting someone it does not fund is free and merely asymmetric.
    """
    warnings = []
    for address in sorted(funded - counted):
        warnings.append(
            f"ledgers.{ledger}: you fund {address} but do not count it. You pay for a "
            "contribution you do not recognise, and earn no credit of your own for it."
        )
    for address in sorted(counted - funded):
        warnings.append(
            f"ledgers.{ledger}: you count {address} but do not fund it. Peers that fund "
            "it gain standing with you; you gain none with them for it."
        )
    return warnings


def report(now: Optional[int] = None) -> dict:
    """Everything this command knows, as the JSON the TUI reads."""
    from src.database.sql_connection import SQLConnection
    from src.payment_system.contracts import envs
    from src.payment_system.donations import config, credit, indexer
    from src.utils.config import ConfigManager

    now = int(time.time()) if now is None else now
    sql = SQLConnection()

    debts = {
        (row["ledger"], row["token_id"]): row["owed"]
        for row in sql.donation_debts()
    }
    # What each wallet has been credited, per ledger and per asset. Read from the rows
    # the payout writes rather than from the payment log: a payment row is one output,
    # and what a weight is measured against is the running total.
    credited: Dict[str, Dict[str, Dict[str, str]]] = {}
    for row in sql.donation_credits():
        by_address = credited.setdefault(row["ledger"] or "", {})
        by_address.setdefault(row["address"] or "", {})[row["token_id"] or ""] = str(row["paid"])

    paid: Dict[str, dict] = {}
    for row in sql.get_donation_payments(limit=1000):
        entry = paid.setdefault(row.get("ledger") or "", {"mu": 0, "count": 0})
        try:
            entry["mu"] += int(row.get("amount_mu") or 0)
        except (TypeError, ValueError):
            pass
        entry["count"] += 1

    ledger_tags = sorted(
        set(envs.donation_scanners())
        | {ledger for ledger, _ in debts}
        | set(paid)
    )

    ledgers = []
    warnings: List[str] = []
    for ledger in ledger_tags:
        pay = config.pay_wallets(ledger)
        count = config.credit_wallets(ledger)
        from src.payment_system.donations.split import normalised as normalise

        pay_normalised = {w.address: w.weight for w in normalise(pay)}
        count_normalised = config.credit_weights(ledger)
        funded = {w.address for w in pay}
        counted = {w.address for w in count}
        warnings.extend(_coherence_warnings(ledger, funded, counted))

        owed = {
            asset: str(amount)
            for (tag, asset), amount in debts.items()
            if tag == ledger
        }
        ledgers.append({
            "ledger": ledger,
            "percentage": str(config.percentage(ledger, "")),
            "min_transfer": str(config.min_transfer(ledger, "")),
            # Per asset, for the ones the operator declared beyond the native unit.
            # Both figures are per payment *method*, so a single pair per ledger is the
            # native unit's and says nothing about a token settling through the same
            # contract -- which may donate a different share, with a different floor.
            "assets": {
                asset: {
                    "percentage": str(config.percentage(ledger, asset)),
                    "min_transfer": str(config.min_transfer(ledger, asset)),
                }
                for asset in config.assets(ledger)
            },
            "min_confirmations": config.min_confirmations(ledger),
            "owed_native": owed,
            "paid_mu": paid.get(ledger, {}).get("mu", 0),
            "paid_count": paid.get(ledger, {}).get("count", 0),
            "scan_tip": sql.donation_scan_tip(ledger),
            "pay_wallets": [
                _wallet_json(w, pay_normalised, counted, credited.get(ledger, {}))
                for w in pay
            ],
            "credit_wallets": [_wallet_json(w, count_normalised, funded) for w in count],
        })

    weight = ConfigManager().get("balancers.DONATION_WEIGHT", 0.3)
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        weight = 0.3

    bonuses = credit.bonus_by_peer()
    peers = sorted(
        (
            {
                "peer_id": peer_id,
                "bonus": bonus,
                # What the bonus is worth where it is spent: the balancer adds this to a
                # score that is otherwise -ln(price), so it is also the largest price
                # premium this credit lets the peer beat.
                "score_term": weight * bonus,
            }
            for peer_id, bonus in bonuses.items()
        ),
        key=lambda entry: entry["bonus"],
        reverse=True,
    )

    return {
        "read_at": now,
        "donation_weight": weight,
        "ledgers": ledgers,
        "peers": peers,
        "unattributed_donors": indexer.unattributed_donors(),
        "warnings": warnings,
    }


def _print_report(data: dict) -> None:
    print("Donations")
    print("=" * 50)
    for ledger in data["ledgers"]:
        print(f"\n{ledger['ledger']}: donating {ledger['percentage']} of incoming payments")
        owed = ledger["owed_native"] or {}
        if owed:
            for asset, amount in sorted(owed.items()):
                print(f"  Accrued, not yet paid: {amount} ({asset}, smallest unit)")
        else:
            print("  Accrued, not yet paid: nothing")
        print(
            f"  Paid out: {ledger['paid_mu']} MU over {ledger['paid_count']} transaction(s)"
        )
        print(f"  Minimum payout: {ledger['min_transfer']} (whole units)")
        for asset, terms in sorted((ledger.get("assets") or {}).items()):
            print(
                f"    {asset}: donating {terms['percentage']}, minimum payout "
                f"{terms['min_transfer']} (whole units)"
            )
        if ledger["pay_wallets"]:
            print("  Funding:")
            for wallet in ledger["pay_wallets"]:
                print(
                    f"    {wallet['address']}  weight {wallet['weight']} "
                    f"(share {wallet['normalised']})"
                )
                # What has actually reached it, which is what makes the weight above a
                # claim an operator can check rather than take on trust.
                reached = wallet.get("paid_native") or {}
                if reached:
                    for asset, amount in sorted(reached.items()):
                        print(
                            f"      paid so far: {amount} "
                            f"({asset or 'native'}, smallest unit)"
                        )
                else:
                    print("      paid so far: nothing yet")
        else:
            print("  Funding: nobody")
        if ledger["credit_wallets"]:
            print("  Counting:")
            for wallet in ledger["credit_wallets"]:
                print(
                    f"    {wallet['address']}  weight {wallet['weight']} "
                    f"(share {wallet['normalised']})"
                )
        else:
            print("  Counting: nobody")

    if data["peers"]:
        print(f"\nDonation credit, at DONATION_WEIGHT {data['donation_weight']}:")
        for peer in data["peers"]:
            print(
                f"  {peer['peer_id']}  bonus {peer['bonus']:.4f} "
                f"(+{peer['score_term']:.4f} to its score)"
            )
    else:
        print("\nNo donation is recognised yet: nothing indexed, or nobody counted.")

    if data["unattributed_donors"]:
        print(
            f"\n{len(data['unattributed_donors'])} donor address(es) map to no known peer "
            "and are credited to nobody yet."
        )
    for warning in data["warnings"]:
        print(f"\n! {warning}")


def donations(argv: Optional[List[str]] = None) -> bool:
    """Print what this node donates and what it counts. ``nodo donations [--json]``."""
    argv = list(argv or [])
    as_json = "--json" in argv

    try:
        data = report()
    except Exception as e:
        if as_json:
            # Shaped like a report, so a reader never branches on which object it got.
            json.dump({"error": str(e), "read_at": int(time.time())}, sys.stdout)
            print()
        else:
            print(f"Could not read the donation state: {e}")
        return _flushed(False)

    if as_json:
        json.dump(data, sys.stdout)
        print()
    else:
        _print_report(data)
    return _flushed(True)


def _flushed(result: bool) -> bool:
    """Return ``result`` with stdout on the wire: ``nodo.py`` exits via ``os._exit``."""
    sys.stdout.flush()
    return result
