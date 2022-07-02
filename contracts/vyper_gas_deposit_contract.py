from importlib.resources import Package


# Singleton class
class VyperDepositContractInterface:
    def __init__(self):
        self.contract_address = None



    def process_payment(self, amount: int, peer_id: int) -> str:
        print("Processing payment...")
        return '0x0000000000000000000000000000000000000000'


    def payment_process_validator(self, amount: int, peer_id: int) -> bool:
        print("Validating payment...")
        return True


def process_payment(amount: int, peer_id: int) -> str:
    return VyperDepositContractInterface().process_payment(amount, peer_id)

def payment_process_validator(amount: int, peer_id: int) -> bool:
    return VyperDepositContractInterface().payment_process_validator(amount, peer_id)