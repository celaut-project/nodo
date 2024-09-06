import os
import sqlite3
from hashlib import sha3_256

from src.payment_system.contracts.ethereum.deposit_contract.interface import DIR
from web3 import Web3

from src.payment_system.contracts.ethereum.envs import ETH_LEDGER, ETH_PROVIDER
from src.utils.env import EnvManager, SHA3_256_ID

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

def seed(private_key=None):

    if not private_key:
        w3 = Web3()
        account = w3.eth.account.create()
        private_key = w3.to_hex(account._private_key)
        print("Wallet public key:", account.address)
    print("Wallet private key:", private_key)

    # Connect to the SQLite database
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # LEDGER
    ledger: str = ETH_LEDGER
    cursor.execute("INSERT OR IGNORE INTO ledger (id, private_key) VALUES (?,?)",
                   (ledger, private_key))

    cursor.execute("INSERT INTO ledger_provider (uri, ledger_id) VALUES (?,?)",
                   (ETH_PROVIDER, ledger))

    # CONTRACT
    contract: bytes = open(os.path.join(DIR, 'bytecode'), 'rb').read()
    contract_hash: str = sha3_256(contract).hexdigest()
    hash_type: str = SHA3_256_ID.hex()
    cursor.execute("INSERT OR IGNORE INTO contract (hash, hash_type, contract) VALUES (?,?,?)",
                   (contract_hash, hash_type, contract))

    print(f"Contrato {contract_hash}. \nYou still need to deploy the contract on the network specified in eth/env.py")

    # DESPLIEGUE DE UN CONTRATO FANTASMA, PARA SIMULAR EL PROCESO DE PAGOS.
    cursor.execute("INSERT INTO contract_instance (address, ledger_id, contract_hash) VALUES (?,?,?)",
                   ("0xaddress", ETH_LEDGER, contract_hash))

    conn.commit()
    conn.close()
