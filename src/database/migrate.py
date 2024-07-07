import sqlite3
import os
from src.utils.env import DATABASE_FILE, STORAGE

def create_directory(path):
    """Ensure the storage directory exists."""
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"Directory created at: {path}")
    else:
        print(f"Directory already exists at: {path}")

def connect_to_database(db_file):
    """Connect to the SQLite database."""
    try:
        conn = sqlite3.connect(db_file)
        print("Connected to database.")
        return conn
    except sqlite3.Error as e:
        print(f"Error connecting to database: {e}")
        return None

def create_tables(cursor):
    """Create tables in the SQLite database."""
    tables = {
        "peer": '''
            CREATE TABLE IF NOT EXISTS peer (
                id TEXT PRIMARY KEY,
                token TEXT,
                metadata BLOB,
                app_protocol BLOB,
                client_id TEXT,
                gas INTEGER
            )
        ''',
        "clients": '''
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                gas INTEGER,
                last_usage FLOAT NULL
            )
        ''',
        "slot": '''
            CREATE TABLE IF NOT EXISTS slot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                internal_port INTEGER,
                transport_protocol BLOB,
                peer_id TEXT,
                FOREIGN KEY (peer_id) REFERENCES peer (id)
            )
        ''',
        "uri": '''
            CREATE TABLE IF NOT EXISTS uri (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT,
                port INTEGER,
                slot_id INTEGER,
                FOREIGN KEY (slot_id) REFERENCES slot (id)
            )
        ''',
        "contract": '''
            CREATE TABLE IF NOT EXISTS contract (
                hash TEXT PRIMARY KEY,
                hash_type TEXT,
                contract BLOB
            )
        ''',
        "ledger": '''
            CREATE TABLE IF NOT EXISTS ledger (
                id TEXT PRIMARY KEY,
                private_key TEXT NULL
            )
        ''',
        "ledger_provider": '''
            CREATE TABLE IF NOT EXISTS ledger_provider (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uri TEXT,
                ledger_id TEXT,
                FOREIGN KEY (ledger_id) REFERENCES peer (id)
            )
        ''',
        "contract_instance": '''
            CREATE TABLE IF NOT EXISTS contract_instance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT,
                ledger_id TEXT,
                contract_hash TEXT,
                peer_id TEXT NULL,
                FOREIGN KEY (ledger_id) REFERENCES ledger (id),
                FOREIGN KEY (contract_hash) REFERENCES contract (hash),
                FOREIGN KEY (peer_id) REFERENCES peer (id)
            )
        ''',
        "internal_services": '''
            CREATE TABLE IF NOT EXISTS internal_services (
                id TEXT PRIMARY KEY,
                ip TEXT,
                father_id TEXT,
                gas: INTEGER,
                mem_limit INTEGER
            )
        ''',
        "external_services": '''
            CREATE TABLE IF NOT EXISTS external_services (
                token TEXT PRIMARY KEY,
                token_hash TEXT,
                peer_id TEXT,
                client_id TEXT
            )
        '''
    }

    for table_name, table_sql in tables.items():
        try:
            cursor.execute(table_sql)
            print(f"Created or updated '{table_name}' table.")
        except sqlite3.Error as e:
            print(f"Error creating '{table_name}' table: {e}")

def migrate():
    """Run the migration script."""
    create_directory(STORAGE)
    
    conn = connect_to_database(DATABASE_FILE)
    if conn is None:
        return
    
    with conn:
        cursor = conn.cursor()
        create_tables(cursor)
        conn.commit()
        print("Database schema created and saved.")
    
    conn.close()
    print("Database connection closed.")

if __name__ == "__main__":
    migrate()
