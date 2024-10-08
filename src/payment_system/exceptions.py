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
