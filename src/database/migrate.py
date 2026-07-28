import sqlite3
import os
from src.utils.config import ConfigManager

env_manager = ConfigManager()

DATABASE_FILE = env_manager.get("DATABASE_FILE")
STORAGE = env_manager.get("STORAGE")

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
    # Know that the protocol_stack on the peer table it's a Service.Api.Slot
    tables = {
        "peer": '''
            CREATE TABLE IF NOT EXISTS peer (
                id TEXT PRIMARY KEY,
                protocol_stack BLOB,
                remote_client_id TEXT,
                gas TEXT,
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
                gas TEXT,
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
                content BLOB
            )
        ''',
        "ledger": '''
            CREATE TABLE IF NOT EXISTS ledger (
                hash TEXT PRIMARY KEY,
                content BLOB,
                private_key TEXT NULL,
                double_spending_retry_time DATETIME DEFAULT NULL
            )
        ''',
        "contract_instance": '''
            CREATE TABLE IF NOT EXISTS contract_instance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT,
                ledger_hash TEXT,
                contract_hash TEXT,
                peer_id TEXT NOT NULL,
                gas_price TEXT,
                FOREIGN KEY (ledger_hash) REFERENCES ledger (id),
                FOREIGN KEY (contract_hash) REFERENCES contract (hash),
                FOREIGN KEY (peer_id) REFERENCES peer (id),
                UNIQUE (address, ledger_hash, contract_hash, peer_id)
            )
        ''',
        "local_instances": '''
            CREATE TABLE IF NOT EXISTS local_instances (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                ip TEXT,
                father_id TEXT,
                gas TEXT,
                mem_limit INTEGER,
                disk_space INTEGER,
                serialized_instance TEXT,
                service_id TEXT,
                virtualizer TEXT DEFAULT NULL,
                envs TEXT DEFAULT NULL
            )
        ''',
        "delegated_instances": '''
            CREATE TABLE IF NOT EXISTS delegated_instances (
                token_delegation TEXT PRIMARY KEY,
                id TEXT,
                peer_id TEXT,
                father_id TEXT,
                serialized_instance TEXT,
                service_id TEXT
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

    # Additive column migrations for databases created before a column existed.
    # `CREATE TABLE IF NOT EXISTS` never alters an existing table, so new columns
    # must be back-filled here. Each entry is idempotent (skipped when present).
    ensure_columns(cursor, "local_instances", {"envs": "TEXT DEFAULT NULL"})


def ensure_columns(cursor, table_name: str, columns: dict) -> None:
    """Add any missing ``columns`` to ``table_name`` (idempotent).

    ``columns`` maps column name -> its SQL declaration (e.g. ``"TEXT DEFAULT NULL"``).
    A column already present is left untouched. Safe to run on every startup.
    """
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing = {row[1] for row in cursor.fetchall()}  # row[1] = column name
    for column, declaration in columns.items():
        if column in existing:
            continue
        try:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {declaration}")
            print(f"Added column '{column}' to '{table_name}' table.")
        except sqlite3.Error as e:
            print(f"Error adding column '{column}' to '{table_name}': {e}")

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
