import sqlite3

# Connect to an existing database
conn = sqlite3.connect('database.sqlite')
print("Connected to database.")

# Create a cursor
cursor = conn.cursor()

# Add the "Peer" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS peer (
        id TEXT PRIMARY KEY,
        token TEXT,
        metadata BLOB,
        app_protocol BLOB
    )
''')
print("Created 'peer' table.")

# Add the "Slot" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS slot (
        id INTEGER PRIMARY KEY AUTOINCREMENT ,
        internal_port INTEGER,
        transport_protocol BLOB,
        peer_id TEXT,
        FOREIGN KEY (peer_id) REFERENCES peer (id)
    )
''')
print("Created 'slot' table.")

# Add the "Uri" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS uri (
        id INTEGER PRIMARY KEY AUTOINCREMENT ,
        ip TEXT,
        port INTEGER,
        slot_id INTEGER,
        FOREIGN KEY (slot_id) REFERENCES slot (id)
    )
''')
print("Created 'uri' table.")

# Add the "Contract" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS contract (
        hash TEXT PRIMARY KEY,
        hash_type TEXT,
        contract BLOB
    )
''')
print("Created 'contract' table.")

# Add the "Ledger" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY,
        private_key TEXT NULL
    )
''')
print("Created 'ledger' table.")

# Add the "Ledger Provider" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS ledger_provider (
        id INTEGER PRIMARY KEY AUTOINCREMENT ,
        uri TEXT,
        ledger_id TEXT,
        FOREIGN KEY (ledger_id) REFERENCES peer (id)
    )
''')
print("Created 'ledger' table.")

# Add the "Contract Instance" table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS contract_instance (
        id INTEGER PRIMARY KEY AUTOINCREMENT ,
        address TEXT,
        ledger_id TEXT,
        contract_hash TEXT,
        peer_id INTEGER,
        FOREIGN KEY (ledger_id) REFERENCES ledger (id),
        FOREIGN KEY (contract_hash) REFERENCES contract (hash),
        FOREIGN KEY (peer_id) REFERENCES peer (id)
    )
''')
print("Created 'contract_instance' table.")

# Save the changes and close the connection
conn.commit()
conn.close()
print("Database connection closed.")
