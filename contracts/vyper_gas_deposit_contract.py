from importlib.resources import Package

def process_payment(amount: int, peer_id: int) -> str:
    print("Processing payment...")
    return '0x0000000000000000000000000000000000000000'


def payment_process_validator(amount: int, peer_id: int, tx_id: str) -> bool:
    print("Validating payment...")
    return True
