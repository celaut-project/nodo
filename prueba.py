import sqlite3

peer_id: str = None
contract_hash: str = "c66790fb3dfb5db949335d983b7c3f717bed7e348f340c42fa7bc9f65e9555e6"

conn = sqlite3.connect('database.sqlite')
cursor = conn.cursor()

# Retrieve the peer instance from the 'peer' table
if peer_id:
    cursor.execute(
        "SELECT address, ledger_id "
        "FROM contract_instance "
        "WHERE contract_hash = ? "
        "AND peer_id = ?",
        (contract_hash, peer_id)
    )
else:
    cursor.execute(
        "SELECT address, ledger_id "
        "FROM contract_instance "
        "WHERE contract_hash = ? "
        "AND peer_id IS NULL",
        (contract_hash,)
    )


result = cursor.fetchone()
print(result)
