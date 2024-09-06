import os.path
import sqlite3
from hashlib import sha3_256

from src.payment_system.contracts.ethereum.deposit_contract.interface import DIR
from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account

from src.payment_system.contracts.ethereum.envs import ETH_LEDGER, ETH_PROVIDER, PARITY_FACTOR
from src.database.access_functions.ledgers import get_private_key_from_ledger
from src.utils.env import EnvManager

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")


def __deploy_contract(provider_url: str, bytecode: bytes, abi: str) -> str:
    # Conectarse al proveedor
    web3 = Web3(Web3.HTTPProvider(provider_url))

    # En caso de conectar con una red Proof of Authority
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)

    # Obtener la cuenta de despliegue
    priv = get_private_key_from_ledger(ETH_LEDGER)
    account = Account.from_key(priv).address
    print(f"Desplegando contrato por parte de la cuenta {account} en {ETH_LEDGER} - {web3.eth.chain_id}")

    # Crear objeto de contrato
    contract = web3.eth.contract(abi=abi, bytecode=bytecode)

    # Desplegar el contrato
    print(f"gas price -> {web3.eth.gas_price}")
    transaction = contract.constructor(PARITY_FACTOR).build_transaction({'gas': 2000000, 'gasPrice': web3.eth.gas_price})
    transaction.update({'from': account,
                        'nonce': web3.eth.get_transaction_count(account),
                        'chainId': web3.eth.chain_id
                        })
    signed = web3.eth.account.sign_transaction(transaction, priv)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction).hex()
    print(f'Transaction hash: {tx_hash}')

    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    print(f"Receipt tx {tx_receipt}")
    # Obtener la dirección del contrato desplegado
    contract_address = tx_receipt['contractAddress']

    print(f"Direccion del contrato {contract_address}")

    return contract_address


def deploy():

    # Connect to the SQLite database
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # READ CONTRACT BYTECODE
    contract: bytes = open(os.path.join(DIR, 'bytecode'), 'rb').read()
    abi: str = open(os.path.join(DIR, 'abi.json'), 'r').read()
    contract_hash: str = sha3_256(contract).hexdigest()

    # CONTRACT DEPLOYED
    address: str = __deploy_contract(provider_url=ETH_PROVIDER, bytecode=contract, abi=abi)
    cursor.execute("INSERT INTO contract_instance (address, ledger_id, contract_hash) VALUES (?,?,?)",
                   (address, ETH_LEDGER, contract_hash))

    print(f"Dirección del contrato desplegado en {ETH_LEDGER} {address}")

    conn.commit()
    conn.close()
