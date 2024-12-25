import sqlite3
import os
from src.utils.env import EnvManager

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
STORAGE = env_manager.get_env("STORAGE")

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
                metadata BLOB NULL,
                app_protocol BLOB,
                client_id TEXT,
                gas_mantissa INTEGER,
                gas_exponent INTEGER,
                gas_last_update DATETIME DEFAULT NULL,
                reputation_proof_id TEXT,
                reputation_score INTEGER,
                reputation_index INTEGER,
                last_index_on_ledger INTEGER
            )
        ''',
        "clients": '''
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                gas_mantissa INTEGER,
                gas_exponent INTEGER,
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
                private_key TEXT NULL,
                double_spending_retry_time DATETIME DEFAULT NULL
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
                peer_id TEXT NOT NULL,
                FOREIGN KEY (ledger_id) REFERENCES ledger (id),
                FOREIGN KEY (contract_hash) REFERENCES contract (hash),
                FOREIGN KEY (peer_id) REFERENCES peer (id),
                UNIQUE (address, ledger_id, contract_hash, peer_id)
            )
        ''',
        "internal_services": '''
            CREATE TABLE IF NOT EXISTS internal_services (
                id TEXT PRIMARY KEY,
                ip TEXT,
                father_id TEXT,
                gas_mantissa INTEGER,
                gas_exponent INTEGER,
                mem_limit INTEGER,
                serialized_instance TEXT
            )
        ''',
        "external_services": '''
            CREATE TABLE IF NOT EXISTS external_services (
                token TEXT PRIMARY KEY,
                token_hash TEXT,
                peer_id TEXT,
                client_id TEXT,
                serialized_instance TEXT
            )
        ''',
        "tunnels": '''
            CREATE TABLE IF NOT EXISTS tunnels (
                id TEXT PRIMARY KEY,
                uri TEXT,
                service TEXT,
                live BOOLEAN
            )
        ''',
        "deposit_tokens": '''
            CREATE TABLE IF NOT EXISTS deposit_tokens (
                id TEXT PRIMARY KEY,
                client_id TEXT,
                status TEXT CHECK( status IN ('pending', 'payed', 'rejected') ) NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients (id)
            )
        ''',
        "energy_consumption": '''
            CREATE TABLE IF NOT EXISTS energy_consumption (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                cpu_percent REAL,
                memory_usage REAL,
                power_consumption REAL,
                cost REAL
            )
        ''',
        "monitoring_config": '''
            CREATE TABLE IF NOT EXISTS monitoring_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_power_limit REAL,
                cost_per_kwh REAL,
                last_updated DATETIME
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
