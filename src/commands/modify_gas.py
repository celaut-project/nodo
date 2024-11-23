from src.manager.manager import modify_gas_deposit


def modify_gas(instance: str, gas: int, decrement: bool=False):
    if decrement: gas *= -1
    result, msg = modify_gas_deposit(gas_amount=gas, service_token=instance)
    if result:
        print(f"Service instance {instance} gas amount modified.")
    else:
        print(f"Something was wrong: {msg}.")
