import os.path
from hashlib import sha256
import pymongo
from src.utils.env import MONGODB
from eth_account import Account


def seed(private_key=None):

    if not private_key:
        from web3 import Web3
        w3 = Web3()
        account = w3.eth.account.create()
        private_key = w3.toHex(account.privateKey)
        print("Dirección de la billetera:", account.address)
        print("Clave privada de la billetera:", private_key)

    mongo = pymongo.MongoClient(
            "mongodb://"+MONGODB+"/"
        )["mongo"]["contracts"]

    mongo.insert_one({
            "ledger": "fuji",
            "priv": private_key,
            "providers": [
                "https://api.avax-test.network/ext/bc/C/rpc",
            ]
        })

    # Deploy contract.
    mongo.insert_one({
            "contract_hash": sha256(open('contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()).digest(),
            "instances": [
                {
                    "ledger": "fuji",
                    "contract_addr": "0x6639fdB1eb6f0D42577c73fB6807ee15B1cc1784",
                },
            ]
        })