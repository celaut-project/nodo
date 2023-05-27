import sqlite3

def seed_database():
    # Connect to the SQLite database
    conn = sqlite3.connect('database.sqlite')
    cursor = conn.cursor()

    # Seed the "peer" table
    cursor.execute("INSERT INTO peer (id, token, metadata, app_protocol) VALUES (?, ?, ?, ?)",
                   ('peer1', 'token1', 'metadata1', 'protocol1'))
    cursor.execute("INSERT INTO peer (id, token, metadata, app_protocol) VALUES (?, ?, ?, ?)",
                   ('peer2', 'token2', 'metadata2', 'protocol2'))

    # Seed the "slot" table
    cursor.execute("INSERT INTO slot (id, internal_port, transport_protocol, peer_id) VALUES (?, ?, ?, ?)",
                   (1, 8080, 'tcp', 'peer1'))
    cursor.execute("INSERT INTO slot (id, internal_port, transport_protocol, peer_id) VALUES (?, ?, ?, ?)",
                   (2, 8081, 'udp', 'peer2'))

    # Seed the "uri" table
    cursor.execute("INSERT INTO uri (id, ip, port, slot_id) VALUES (?, ?, ?, ?)",
                   (1, '192.168.0.1', 5000, 1))
    cursor.execute("INSERT INTO uri (id, ip, port, slot_id) VALUES (?, ?, ?, ?)",
                   (2, '192.168.0.2', 6000, 2))

    # Seed the "contract" table
    cursor.execute("INSERT INTO contract (hash, hash_type, contract) VALUES (?, ?, ?)",
                   ('contract_hash1', 'sha256', 'contract_data1'))
    cursor.execute("INSERT INTO contract (hash, hash_type, contract) VALUES (?, ?, ?)",
                   ('contract_hash2', 'sha256', 'contract_data2'))

    # Seed the "ledger" table
    cursor.execute("INSERT INTO ledger (id, private_key) VALUES (?, ?)",
                   ('ledger1', 'private_key1'))
    cursor.execute("INSERT INTO ledger (id, private_key) VALUES (?, ?)",
                   ('ledger2', 'private_key2'))

    # Seed the "ledger_provider" table
    cursor.execute("INSERT INTO ledger_provider (id, uri, ledger_id) VALUES (?, ?, ?)",
                   (1, 'ledger_provider_uri1', 'ledger1'))
    cursor.execute("INSERT INTO ledger_provider (id, uri, ledger_id) VALUES (?, ?, ?)",
                   (2, 'ledger_provider_uri2', 'ledger2'))

    # Seed the "contract_instance" table
    cursor.execute("INSERT INTO contract_instance (id, address, ledger_id, contract_hash, peer_id) VALUES (?, ?, ?, ?, ?)",
                   (1, 'contract_address1', 'ledger1', 'contract_hash1', 'peer1'))
    cursor.execute("INSERT INTO contract_instance (id, address, ledger_id, contract_hash, peer_id) VALUES (?, ?, ?, ?, ?)",
                   (2, 'contract_address2', 'ledger2', 'contract_hash2', 'peer2'))

    # Commit the changes and close the connection
    conn.commit()
    conn.close()
    print("Database seeding completed.")

if __name__ == '__main__':
    seed_database()
