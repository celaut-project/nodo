from hashlib import sha256
import pymongo

mongo = pymongo.MongoClient(
        "mongodb://localhost:27017/"
    )["mongo"]["contracts"]
    
mongo.insert_one({
        "ledger": "fuji",
        "priv": open("fuji.priv", "r").read(),
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