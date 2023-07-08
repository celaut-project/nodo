import sqlite3
from hashlib import sha3_256
from web3 import Web3

from contracts.eth_main.deploy import deploy_contract
from src.utils.env import SHA3_256_ID

ETH_PROVIDER = "https://goerli.infura.io/v3/197fd20680784ab3bd632cda61beb995"


def seed(private_key=None):

    if not private_key:
        w3 = Web3()
        account = w3.eth.account.create()
        private_key = w3.to_hex(account._private_key)
        print("Dirección de la billetera:", account.address)
    print("Clave privada de la billetera:", private_key)

    # Connect to the SQLite database
    conn = sqlite3.connect('database.sqlite')
    cursor = conn.cursor()

    # LEDGER
    ledger: str = "fuji"
    cursor.execute("INSERT OR IGNORE INTO ledger (id, private_key) VALUES (?,?)",
                   (ledger, private_key))

    cursor.execute("INSERT INTO ledger_provider (uri, ledger_id) VALUES (?,?)",
                   (ETH_PROVIDER, ledger))

    # CONTRACT
    contract: bytes = open('contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()
    contract_hash: str = sha3_256(contract).hexdigest()
    hash_type: str = SHA3_256_ID.hex()
    cursor.execute("INSERT OR IGNORE INTO contract (hash, hash_type, contract) VALUES (?,?,?)",
                   (contract_hash, hash_type, contract))

    # CONTRACT DEPLOYED
    address: str = deploy_contract(provider_url=ETH_PROVIDER, bytecode=contract)
    cursor.execute("INSERT INTO contract_instance (address, ledger_id, contract_hash) VALUES (?,?,?)",
                   (address, ledger, contract_hash))

    conn.commit()
    conn.close()
