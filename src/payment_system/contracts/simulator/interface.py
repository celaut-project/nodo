from protos import celaut_pb2, gateway_pb2
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


def process_payment(amount: int, deposit_token: str, ledger: str,
                    contract_address: str) -> celaut_pb2.ContractLedger:
    LOGGER(f"Process simulated payment for token {deposit_token} of {amount}")
    return gateway_pb2.celaut__pb2.ContractLedger(
                ledger=ledger,
                contract_addr=contract_address,
                contract=CONTRACT
            )


def payment_process_validator(amount: int, token: str, ledger: str, contract_addr: str) -> bool:
    return True
