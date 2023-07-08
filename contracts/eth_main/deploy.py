from web3 import Web3


def deploy_contract(provider_url: str, bytecode: bytes) -> str:
    # Conectarse al proveedor
    web3 = Web3(Web3.HTTPProvider(provider_url))

    # Obtener la cuenta de despliegue
    account = web3.eth.accounts[0]  # Utiliza tu propia cuenta o lógica para seleccionar la cuenta de despliegue

    # Crear objeto de contrato
    contract = web3.eth.contract(abi='', bytecode=bytecode)

    # Estimar gas
    gas_estimate = contract.constructor().estimate_gas()

    # Desplegar el contrato
    tx_hash = contract.constructor().transact({'from': account, 'gas': gas_estimate})
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    # Obtener la dirección del contrato desplegado
    contract_address = tx_receipt['contractAddress']

    return contract_address
