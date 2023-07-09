import json
import sqlite3
from hashlib import sha3_256

from web3 import Web3
from eth_account import Account

from contracts.eth_main.envs import ETH_LEDGER, ETH_PROVIDER
from src.utils.utils import get_private_key_from_ledger


def __deploy_contract(provider_url: str, bytecode: bytes, abi) -> str:
    # Conectarse al proveedor
    web3 = Web3(Web3.HTTPProvider(provider_url))

    # Obtener la cuenta de despliegue
    account = Account.from_key(
        get_private_key_from_ledger(ETH_LEDGER)
    ).address
    print(f"Desplegando contrato por parte de la cuenta {account} en {ETH_LEDGER}")

    # Crear objeto de contrato
    contract = web3.eth.contract(abi=abi, bytecode=bytecode)

    print(f"Objeto del contrato {contract}")

    # Estimar gas
    gas_estimate = contract.constructor().estimate_gas()

    print(f"Gas estimado para el despliegue  {gas_estimate}")

    # Desplegar el contrato
    tx_hash = contract.constructor().transact({'from': account, 'gas': gas_estimate})
    print(f"Hash de la transaccion {tx_hash}")
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    print(f"Receipt tx {tx_receipt}")
    # Obtener la dirección del contrato desplegado
    contract_address = tx_receipt['contractAddress']

    print(f"Direccion del contrato {contract_address}")

    return contract_address


def deploy():

    # Connect to the SQLite database
    conn = sqlite3.connect('database.sqlite')
    cursor = conn.cursor()

    # READ CONTRACT BYTECODE
    contract: bytes = open('contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()
    abi: str = json.load(open('contracts/vyper_gas_deposit_contract/abi.json', 'r'))
    contract_hash: str = sha3_256(contract).hexdigest()

    # CONTRACT DEPLOYED
    address: str = __deploy_contract(provider_url=ETH_PROVIDER, bytecode=contract, abi=abi)
    cursor.execute("INSERT INTO contract_instance (address, ledger_id, contract_hash) VALUES (?,?,?)",
                   (address, ETH_LEDGER, contract_hash))

    print(f"Dirección del contrato desplegado en {ETH_LEDGER} {address}")

    conn.commit()
    conn.close()
