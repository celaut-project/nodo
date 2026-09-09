from typing import Tuple

from protos import celaut_pb2
from hashlib import sha3_256
from src.utils.logger import LOGGER

CONTRACT = """
    PAYMENT AGREEMENT

    This Payment Agreement ("Agreement") is made and entered into on [Date] by and between:

    Party A: [Full Name/Company Name], with a business address at [Address], hereinafter referred to as the "Payee."

    Party B: [Full Name/Company Name], with a business address at [Address], hereinafter referred to as the "Payer."

    1. Payment Terms:

    1.1. The Payer agrees to pay the Payee the total sum of [Amount] in exchange for [Description of Service/Product].

    1.2. Payment shall be made in the following manner:

        Amount: [Insert Amount]
        Due Date: [Insert Date or Payment Schedule]
        Payment Method: [Bank Transfer, Check, etc.]

    2. Late Payments:

    2.1. In the event that payment is not made by the due date specified in this Agreement, the Payer agrees to pay a late fee of [Percentage]% of the outstanding balance per [Week/Month] that the payment is delayed.

    3. Termination:

    3.1. This Agreement may be terminated by mutual written consent of both parties.

    3.2. If the Payer fails to make the payment as outlined above, the Payee reserves the right to terminate this Agreement and seek legal remedies.

    4. Dispute Resolution:

    4.1. In the event of a dispute arising from this Agreement, both parties agree to resolve the issue amicably through mediation or arbitration before pursuing legal action.

    5. Governing Law:

    5.1. This Agreement shall be governed by and construed in accordance with the laws of [State/Country].

    6. Entire Agreement:

    6.1. This Agreement constitutes the entire understanding between the parties and supersedes all prior discussions, agreements, or understandings of any kind.

    IN WITNESS WHEREOF, the parties have executed this Agreement as of the date written below.

    Payee Signature: _______________________________
    Name: [Payee's Full Name]
    Date: [Date]

    Payer Signature: _______________________________
    Name: [Payer's Full Name]
    Date: [Date]
""".encode('utf-8')
CONTRACT_HASH = sha3_256(CONTRACT).hexdigest()
LEDGER = "simulated"
# A simulated payment settles on no chain, so nothing here can be double-spent and
# there is no box for a sweep to consume.
needs_unspent_proof = False
is_demo = True


def ledger() -> celaut_pb2.Contract.Ledger:
    """A ledger tag that names itself for what it is.

    Carried so the registry can treat this contract like any other; nothing resolves a
    node URL or a chain from it, because there is neither.
    """
    return celaut_pb2.Contract.Ledger(
        tags=[LEDGER],
        prose="Simulated payments: nothing settles, nothing moves.",
        formal=b"",
    )


def init() -> None:
    """Register nothing, deliberately.

    Every other contract writes a LOCAL row here so peers can read what this node
    accepts. This one must not: a simulated payment moves no money, so a peer that read
    it out of `GetPeerInfo` and paid through it would have paid into nothing. The flag
    exists to let a node exercise its own payment path, not to offer that path to
    others -- which is why `envs.DEMOS` exists on the *payer's* side only.
    """
    return None


def manager() -> None:
    """Nothing to sweep and nothing owed: no wallet, no chain, no fee."""
    return None


def process_payment(amount: int, deposit_token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> celaut_pb2.Contract:
    LOGGER(f"Process simulated payment for token {deposit_token} of {amount}")
    return celaut_pb2.Contract(
                ledger=ledger,
                token_id="",
                script=script,
                contract=CONTRACT
            )


def payment_process_validator(amount: int, token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> bool:
    return True

def check_sender_balance(amount: int) -> bool:
    return True


def settlement_floors_mu() -> Tuple[int, int]:
    """``(fee, smallest payable output)``, both zero: nothing here reaches a chain.

    A simulated payment costs nothing and has no minimum output, so this contract imposes
    no floor on how small a deposit may be. Answering honestly rather than borrowing
    Ergo's box-value floor is the point of asking the contract at all -- deposit sizing
    used to import Ergo's constants directly, so a node running purely on this contract
    still sized its deposits against a chain it never touched.
    """
    return 0, 0