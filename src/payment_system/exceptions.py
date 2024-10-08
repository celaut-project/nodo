
class DoubleSpendingAttempt(Exception):
    """Exception raised for an attempt at double spending."""
    
    def __init__(self, ledger="'no specified'", message="Double spending attempt detected"):
        self.ledger = ledger
        self.message = f"{message} on ledger {ledger}"
        super().__init__(self.message)