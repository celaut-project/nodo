import sqlite3
from hashlib import sha3_256
from web3 import Web3
from src.utils.env import SHA3_256_ID


def seed(private_key=None):

    if not private_key:
        w3 = Web3()
        account = w3.eth.account.create()
        private_key = w3.toHex(account.privateKey)
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
                   ("https://api.avax-test.network/ext/bc/C/rpc", "ledger"))

    # CONTRACT
    contract: bytes = open('contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()
    contract_hash: bytes = sha3_256(contract).digest()
    hash_type: bytes = SHA3_256_ID
    cursor.execute("INSERT OR IGNORE INTO contract (hash, hash_type, contract) VALUES (?,?,?)",
                   (contract_hash, hash_type, contract))

    # CONTRACT DEPLOYED
    address: str = "0x6639fdB1eb6f0D42577c73fB6807ee15B1cc1784"
    cursor.execute("INSERT INTO contract_instance (address, ledger_id, contract_hash) VALUES (?,?,?)",
                   (address, ledger, contract_hash))

    conn.commit()
    conn.close()
