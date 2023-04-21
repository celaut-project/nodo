from hashlib import sha256
import pymongo
from src.utils.env import MONGODB

def seed():

    mongo = pymongo.MongoClient(
            "mongodb://"+MONGODB+"/"
        )["mongo"]["contracts"]

    mongo.insert_one({
            "ledger": "fuji",
            "priv": open("contracts/eth_main/fuji.priv", "r").read()[:-1],
            "providers": [
                "https://api.avax-test.network/ext/bc/C/rpc",
            ]
        })

    mongo.insert_one({
            "contract_hash": sha256(open('contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()).digest(),
            "instances": [
                {
                    "ledger": "fuji",
                    "contract_addr": "0x6639fdB1eb6f0D42577c73fB6807ee15B1cc1784",
                },
            ]
        })