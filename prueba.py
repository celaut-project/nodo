import sqlite3

peer_id: str = "c1f32d2f-12d1-4221-838f-1ddf3318355e"
contract_hash: str = "c66790fb3dfb5db949335d983b7c3f717bed7e348f340c42fa7bc9f65e9555e6"

conn = sqlite3.connect('database.sqlite')
cursor = conn.cursor()

# Retrieve the peer instance from the 'peer' table
cursor.execute(
    "SELECT l.id, ci.address "
    "FROM ledger l "
    "JOIN contract_instance ci "
    "ON l.id == ci.ledger_id "
    "WHERE ci.peer_id = ? "
    "AND ci.contract_hash = ? ",
    (peer_id, contract_hash,)
)

result = cursor.fetchone()
print(result)
