from src.database.sql_connection import SQLConnection

class DoubleSpendingAttempt(Exception):
    """Exception raised for an attempt at double spending."""
    
    def __init__(self, ledger=None, message="Double spending attempt detected"):
        self.ledger = ledger

        # Message
        ledger = "'no specified'"
        self.message = f"{message} on ledger {ledger}. Wait 10 minutes time more to try."
        super().__init__(self.message)

        # Update
        if self.ledger:
            SQLConnection().update_double_attempt_retry_time_on_ledger(ledger=self.ledger)


# The words a payer looks for in the error of a `Payable` that came too early (#626).
NOT_CONFIRMED_YET = "not confirmed yet"


class PaymentNotConfirmed(Exception):
    """A transaction pays the deposit, but it has fewer confirmations than the payee needs.

    Not a refusal: the deposit token stays 'pending', and the payer can call `Payable`
    again when the transaction has more confirmations, before the token expires (#626).
    """

    def __init__(self, token: str, tx_id: str, confirmations, wanted: int):
        self.token = token
        self.tx_id = tx_id
        self.confirmations = confirmations
        self.wanted = wanted
        have = "an unknown number of" if confirmations is None else str(confirmations)
        super().__init__(
            f"Payment {NOT_CONFIRMED_YET}: transaction {tx_id} has {have} "
            f"confirmation(s) and this node needs {wanted}. The deposit token stays "
            "pending. Call Payable again before it expires."
        )
