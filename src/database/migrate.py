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
    # Know that the advertisement on the peer table is a serialized celaut.Peer,
    # carrying what a peer declares node-wide (payment contracts and rates). Its
    # addresses live in the `uri` table, one row each, since they are queried by
    # ip/port rather than read back as a whole.
    tables = {
        "peer": '''
            CREATE TABLE IF NOT EXISTS peer (
                id TEXT PRIMARY KEY,
                advertisement BLOB,
                remote_client_id TEXT,
                balance_mu TEXT,
                balance_last_update DATETIME DEFAULT NULL,
                reputation_proof_id TEXT,
                reputation_score INTEGER,
                reputation_index INTEGER,
                last_index_on_ledger INTEGER,
                last_ts INTEGER DEFAULT NULL
            )
        ''',
        "clients": '''
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                balance_mu TEXT,
                last_usage FLOAT NULL,
                unmetered INTEGER NOT NULL DEFAULT 0
            )
        ''',
        "uri": '''
            CREATE TABLE IF NOT EXISTS uri (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id TEXT,
                ip TEXT,
                port INTEGER,
                expiry_unix_timestamp INTEGER DEFAULT NULL,
                transport TEXT DEFAULT NULL,
                protocol_stack BLOB DEFAULT NULL,
                FOREIGN KEY (peer_id) REFERENCES peer (id)
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
                mu_per_unit TEXT,
                FOREIGN KEY (ledger_hash) REFERENCES ledger (id),
                FOREIGN KEY (contract_hash) REFERENCES contract (hash),
                FOREIGN KEY (peer_id) REFERENCES peer (id),
                UNIQUE (address, ledger_hash, contract_hash, peer_id)
            )
        ''',
        # mem_limit, disk_space and the CFS pair (cpu_period/cpu_quota) are what the
        # maintenance tick prices an instance by, so all four have to be here: the tick
        # reads this row, not the service's manifest. Storing memory and disk but not
        # CPU meant compute was never billed on the recurring path, whatever the price.
        "local_instances": '''
            CREATE TABLE IF NOT EXISTS local_instances (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                ip TEXT,
                father_id TEXT,
                balance_mu TEXT,
                mem_limit INTEGER,
                disk_space INTEGER,
                cpu_period INTEGER,
                cpu_quota INTEGER,
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
        ''',
        "forced_execution_peer": '''
            CREATE TABLE IF NOT EXISTS forced_execution_peer (
                token TEXT PRIMARY KEY,
                peer_id TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''',
        # Per-instance MU burn rate. The maintenance tick already computes what each
        # instance costs for the interval it just held its resources; that charge is
        # sampled here (see SQLConnection.record_instance_consumption) so the TUI can
        # show a spend rate next to the balance. Kept in its own table -- not columns on
        # local_instances -- so it survives instance churn and keeps the hot row small.
        # `mu_per_second` is the running average of the last ~hour of samples; the TUI
        # derives per-minute / per-hour from it and renders in ui.DISPLAY_UNIT.
        "instance_consumption": '''
            CREATE TABLE IF NOT EXISTS instance_consumption (
                instance_id TEXT PRIMARY KEY,
                mu_per_second REAL,
                sample_count INTEGER,
                last_refresh DATETIME,
                FOREIGN KEY (instance_id) REFERENCES local_instances (id)
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
    ensure_columns(cursor, "peer", {
        "last_ts": "INTEGER DEFAULT NULL",
        "advertisement": "BLOB DEFAULT NULL",
    })
    ensure_columns(cursor, "uri", {
        "peer_id": "TEXT DEFAULT NULL",
        "expiry_unix_timestamp": "INTEGER DEFAULT NULL",
        "transport": "TEXT DEFAULT NULL",
        "protocol_stack": "BLOB DEFAULT NULL",
    })
    retire_slot_table(cursor)
    ensure_peer_address_uniqueness(cursor)


def retire_slot_table(cursor) -> None:
    """Move a peer's addresses off the ``slot`` indirection and onto the peer itself.

    ``slot`` grouped a peer's URIs by ``internal_port`` so they could be matched
    against an ``Api.Slot`` of the same port number. A ``Peer.Uri`` now carries its own
    transport and protocol stack, so there is nothing left to match and nothing left
    for the row to hold -- its other column, ``transport_protocol``, was only ever
    written, never read back.

    Idempotent: a no-op once the table is gone.
    """
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='slot'")
        if not cursor.fetchone():
            return
        # Adopt the owning peer before the join disappears. Rows whose slot is already
        # missing are orphans and would be unreachable anyway, so they go.
        cursor.execute(
            "UPDATE uri SET peer_id = (SELECT s.peer_id FROM slot s WHERE s.id = uri.slot_id) "
            "WHERE peer_id IS NULL"
        )
        cursor.execute("DELETE FROM uri WHERE peer_id IS NULL")
        cursor.execute("DROP INDEX IF EXISTS idx_uri_slot_ip_port")
        cursor.execute("DROP INDEX IF EXISTS idx_slot_peer_port")
        cursor.execute("DROP TABLE slot")
        print("Retired the 'slot' table; peer URIs now hang off the peer directly.")
    except sqlite3.Error as e:
        print(f"Error retiring the slot table: {e}")


def ensure_peer_address_uniqueness(cursor) -> None:
    """Enforce one row per (peer, ip, port).

    ``add_peer_uri`` merges a peer's advertisement instead of clearing and reinserting
    it, and it runs concurrently: gRPC serves IntroducePeer on a 30-thread pool, and
    ``_execute`` commits each statement separately, so a plain SELECT-then-INSERT is
    not atomic. This index makes the upsert the database's job rather than a race the
    application hopes to win.

    Existing databases may already hold duplicates (created before this), so de-dup
    first -- keeping the lowest rowid -- or the CREATE fails.
    """
    try:
        cursor.execute('''
            DELETE FROM uri WHERE id > (
                SELECT MIN(k.id) FROM uri k
                WHERE k.peer_id = uri.peer_id AND k.ip = uri.ip AND k.port = uri.port
            )
        ''')
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_uri_peer_ip_port ON uri (peer_id, ip, port)"
        )
    except sqlite3.Error as e:
        print(f"Error enforcing peer address uniqueness: {e}")


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
