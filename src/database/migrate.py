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


# Know that the advertisement on the peer table is a serialized celaut.Peer,
# carrying what a peer declares node-wide (payment contracts and rates). Its
# addresses live in the `uri` table, one row each, since they are queried by
# ip/port rather than read back as a whole.
TABLES = {
    "peer": '''
        CREATE TABLE IF NOT EXISTS peer (
            id TEXT PRIMARY KEY,
            advertisement BLOB,
            remote_client_id TEXT,
            balance_mu TEXT,
            balance_last_update DATETIME DEFAULT NULL,
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
    # A payment method is ledger + contract + **asset**, not ledger + contract. On Ergo
    # one P2PK contract is paid in ERG *and* in any EIP-4 token: same script, same
    # address, same contract_hash, different asset. `token_id` is that third dimension --
    # the reserved symbol of the chain's native unit ("ERG", "BTC") or a token's 64-hex
    # id, which can never collide with a symbol.
    #
    # It is part of the unique key because `mu_per_unit` lives on this row and
    # `add_contract` upserts it: without the asset, a node advertising ERG and SigUSD
    # would overwrite its own ERG rate with the SigUSD one, in the same row, and every
    # peer would convert ERG amounts at the SigUSD rate. Nothing would raise.
    "contract_instance": '''
        CREATE TABLE IF NOT EXISTS contract_instance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT,
            ledger_hash TEXT,
            contract_hash TEXT,
            token_id TEXT NOT NULL DEFAULT '',
            peer_id TEXT NOT NULL,
            mu_per_unit TEXT,
            FOREIGN KEY (ledger_hash) REFERENCES ledger (id),
            FOREIGN KEY (contract_hash) REFERENCES contract (hash),
            FOREIGN KEY (peer_id) REFERENCES peer (id),
            UNIQUE (address, ledger_hash, contract_hash, token_id, peer_id)
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
            envs TEXT DEFAULT NULL,
            arch TEXT DEFAULT NULL
        )
    ''',
    # A delegated instance keeps its deposit in `balance_mu`, in *our* MU, exactly as
    # a local one keeps it in `local_instances.balance_mu`. The father is charged the
    # whole deposit when the child is delegated, and that MU is parked here instead of
    # being absorbed: the maintenance tick spends it down and whatever is left when
    # the instance stops goes back to the father. A deposit with no row to sit on is a
    # deposit nobody can hand back, and a parent that starts and stops delegated
    # children in a loop would then pay a full deposit per iteration and be refunded
    # nothing.
    #
    # `peer_balance_mu` is the child's balance as the *peer* keeps it, in the peer's
    # own MU, as of the last time this node read it. It is a high-water mark, not a
    # mirror: the tick reads the peer's figure again and charges the difference, so
    # what the client pays follows what the peer actually metered rather than what it
    # quoted -- through a price change, a rate change, or a hotplug that made the
    # child bigger than the shape it was quoted at. Storing it unconverted is what
    # makes that possible; a figure converted at write time goes stale the moment
    # either node moves its rate (same reason as `peer.balance_mu`).
    "delegated_instances": '''
        CREATE TABLE IF NOT EXISTS delegated_instances (
            token_delegation TEXT PRIMARY KEY,
            id TEXT,
            peer_id TEXT,
            father_id TEXT,
            serialized_instance TEXT,
            service_id TEXT,
            balance_mu TEXT,
            peer_balance_mu TEXT
        )
    ''',
    "deposit_tokens": '''
        CREATE TABLE IF NOT EXISTS deposit_tokens (
            id TEXT PRIMARY KEY,
            client_id TEXT,
            status TEXT CHECK( status IN ('pending', 'payed', 'rejected') ) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients (id)
        )
    ''',
    # Node energy samples (issue #258). Energy for the interval plus the tariff
    # then in effect; cost is derived on read so a later price change does not
    # rewrite history. RAPL readings are a CPU-package floor (is_floor=1).
    "energy_consumption": '''
        CREATE TABLE IF NOT EXISTS energy_consumption (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            energy_joules REAL,
            watts REAL,
            price_per_kwh REAL,
            currency TEXT,
            backend TEXT,
            is_floor INTEGER
        )
    ''',
    # Latest per-instance share of measured node watts. Same shape as
    # instance_consumption: one hot row, not a time series. Unattributed watts
    # (host, idle, nodo itself) are not stored here — they are node_watts minus
    # the sum of these rows.
    "instance_energy": '''
        CREATE TABLE IF NOT EXISTS instance_energy (
            instance_id TEXT PRIMARY KEY,
            watts REAL,
            share REAL,
            sample_count INTEGER,
            last_refresh DATETIME,
            FOREIGN KEY (instance_id) REFERENCES local_instances (id)
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
    ''',
    # One row per payment this node took part in. Until this table existed a payment
    # left no local trace at all: `add_balance_to_peer` moved a counter and the tx id
    # reached a log line and nothing else, so "what did we send that peer, and when"
    # was unanswerable -- and unanswerable *on purpose* is different from a chain
    # nobody indexed, because the ledger cannot map an address back to a peer id.
    #
    # `status` is what happened, not what we hoped:
    #   communicated  -- outgoing, the peer acknowledged our Payable call
    #   unacknowledged-- outgoing, the transaction was broadcast and the call failed.
    #                    Money left, credit never arrived. The row an operator needs.
    #   accepted      -- incoming, the deposit was validated and credited
    #   rejected      -- incoming, the deposit could not be validated
    #
    # `tx_id` is NULL on incoming rows: an incoming payment is proved by finding an
    # unspent box carrying the deposit token in R4, and the id of the transaction
    # that created the box is not part of that proof. The deposit token is the link
    # (see `tx_history`), and it is stored here.
    # `address` is the counterparty's, as hex propositionBytes -- the same form
    # `contract_instance.address` holds, so the two join. It is NULL on incoming
    # rows, where the only address involved is this node's own wallet.
    # `amount_mu` is TEXT for the same reason every other balance in this schema is:
    # MU exceeds what SQLite stores as an integer.
    # Tunnelled bytes relayed per local calendar day, so `host_limits.MAX_NET_GIB_PER_DAY`
    # is an allowance for the day rather than for the current run of the daemon: a
    # counter living only in memory would reset on every restart, which on a metered
    # connection is the one moment the ceiling was there for. One row per day, written
    # in blocks (see src/utils/host_limits._DailyTraffic), read back at startup and at
    # each midnight rollover.
    "tunnel_traffic": '''
        CREATE TABLE IF NOT EXISTS tunnel_traffic (
            day TEXT PRIMARY KEY,
            bytes INTEGER NOT NULL DEFAULT 0
        )
    ''',
    # What this node was asked for, by local hour (issue #337). The operator chooses the
    # hours this machine works in; this is what makes that choice answerable against
    # something -- which hours anybody asks for, and what closing costs.
    #
    # `hour` is 'YYYY-MM-DDTHH' local, so a lexical sort is a time sort and the last
    # two characters are the hour of the clock -- which is how the TUI folds a month
    # into 24 columns. Local rather than UTC to line up with `activity_window`, the
    # setting it exists to be read against.
    #
    # `instances_held` is the *peak* within the hour, not the mean: the question is what
    # the machine had to fit at once. `mu_charged` is TEXT for the reason every balance
    # in this schema is -- MU exceeds what SQLite stores as an integer.
    #
    # `refused_closed` is the row's reason for existing. A refusal for want of memory
    # would have happened at any hour; one behind a shut window is work the operator
    # declined and could accept by moving an edge, so the two are counted apart.
    "demand_history": '''
        CREATE TABLE IF NOT EXISTS demand_history (
            hour TEXT PRIMARY KEY,
            instances_held INTEGER NOT NULL DEFAULT 0,
            mu_charged TEXT NOT NULL DEFAULT '0',
            admissions INTEGER NOT NULL DEFAULT 0,
            refusals INTEGER NOT NULL DEFAULT 0,
            refused_closed INTEGER NOT NULL DEFAULT 0
        )
    ''',
    "payments": '''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id TEXT DEFAULT NULL,
            direction TEXT CHECK( direction IN ('out', 'in') ) NOT NULL,
            status TEXT CHECK( status IN ('communicated', 'unacknowledged', 'accepted', 'rejected') ) NOT NULL,
            peer_id TEXT DEFAULT NULL,
            client_id TEXT DEFAULT NULL,
            deposit_token TEXT DEFAULT NULL,
            ledger TEXT DEFAULT NULL,
            contract_hash TEXT DEFAULT NULL,
            -- The asset the payment was made IN: a reserved native symbol ("ERG") or a
            -- token's 64-hex id. `amount_mu` is deliberately ledger-neutral, so without
            -- this a row cannot say what money moved -- and one contract settles in
            -- several assets (see contract_instance).
            token_id TEXT DEFAULT NULL,
            address TEXT DEFAULT NULL,
            amount_mu TEXT NOT NULL,
            purpose TEXT DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''',
    # Why each score moved. `peer.reputation_score` is a running total, so a peer
    # sitting at -390 said nothing about whether that was one catastrophe or forty
    # refused calls -- and the amounts are hard-coded at their call sites, which is
    # exactly the kind of number that needs reviewing against what it punishes.
    #
    # `reason` is a stable string from `reputation_system.reasons.Reason`, not free
    # text: the detail views group by it. `score_after` is the running total once
    # this event was applied, so a history reads without replaying it, and stays
    # readable when the totals are later recomputed differently.
    "reputation_events": '''
        CREATE TABLE IF NOT EXISTS reputation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT CHECK( subject_kind IN ('peer', 'service') ) NOT NULL,
            subject_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            score_after INTEGER DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''',
    # A service's score, aggregated over every instance of it that ever ran here.
    # Its own table rather than columns somewhere: a service outlives its instances
    # (that is the point of scoring the service and not the vmachine), and it has no
    # row of its own anywhere else -- services live in the registry, on disk.
    "service_reputation": '''
        CREATE TABLE IF NOT EXISTS service_reputation (
            service_id TEXT PRIMARY KEY,
            reputation_score INTEGER NOT NULL DEFAULT 0,
            reputation_index INTEGER NOT NULL DEFAULT 0
        )
    ''',
    # What this node owes in donations but has not paid yet. One row per payment
    # METHOD -- (ledger, contract, asset) -- because a debt incurred in one asset is
    # not a debt in another and cannot share a counter with it: paying it out would
    # mean converting at whatever rate happens to be configured later.
    #
    # `owed_native` counts the asset's smallest native unit (nanoERG for ERG), never
    # MU: a debt is incurred at the rate of the moment it was incurred, and storing it
    # in MU would let a later rate change retroactively reinterpret it.
    #
    # It is an exact DECIMAL string, not an integer, and that is deliberate. A 2 % cut
    # of a small payment has a fractional part; discarding it would make the effective
    # long-run donation rate drift below the configured one, and always in the node's
    # own favour. The fraction stays here and is paid once it grows into a whole unit.
    "donation_accrual": '''
        CREATE TABLE IF NOT EXISTS donation_accrual (
            ledger TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            token_id TEXT NOT NULL,
            owed_native TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ledger, contract_hash, token_id)
        )
    ''',
    # Donations observed on-chain -- other peers' and our own. This is the credit
    # source of truth, and it is read from the chain by every node independently: a
    # peer telling us what it donated would be self-declared, therefore forgeable.
    #
    # The UNIQUE key is what makes a re-scan idempotent, so the indexer can be dumb
    # about overlapping page boundaries and about reorgs near the tip. `token_id` is
    # part of it because one Ergo transaction can pay the same address in ERG *and* in
    # a token, in the same box -- without it the second asset would collide with the
    # first and be lost.
    #
    # `peer_id` stays NULL until the donor address maps to a peer we know. The row is
    # stored anyway: the peer may be discovered later, and the donation was real when
    # it happened.
    "donations": '''
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger TEXT NOT NULL,
            tx_id TEXT NOT NULL,
            to_address TEXT NOT NULL,
            from_address TEXT NOT NULL,
            peer_id TEXT DEFAULT NULL,
            token_id TEXT NOT NULL,
            amount_native TEXT NOT NULL,
            tx_height INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (ledger, tx_id, to_address, from_address, token_id)
        )
    ''',
    # Where the incremental scan of each counted address got to, so a refresh resumes
    # instead of re-reading a chain's whole history.
    "donation_scan_state": '''
        CREATE TABLE IF NOT EXISTS donation_scan_state (
            ledger TEXT NOT NULL,
            address TEXT NOT NULL,
            last_scanned_height INTEGER NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ledger, address)
        )
    '''
}

# The three ways `payments` is ever read: one peer's history, one client's, and the
# tx-id lookup `tx_history` does per explorer transaction. The TUI runs the first on
# every redraw of a selected peer.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_payments_peer ON payments (peer_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_payments_client ON payments (client_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_payments_tx ON payments (tx_id)",
    # Reputation events are only ever read for one subject at a time, newest first.
    "CREATE INDEX IF NOT EXISTS idx_reputation_events_subject "
    "ON reputation_events (subject_kind, subject_id, created_at)",
    # The balancer aggregates a donor's credit on every routing decision, and the
    # indexer looks a donation up by the address it paid.
    "CREATE INDEX IF NOT EXISTS idx_donations_peer ON donations (peer_id)",
    "CREATE INDEX IF NOT EXISTS idx_donations_from ON donations (ledger, from_address)",
)


def ensure_tables(cursor, names) -> None:
    """Create the named tables (and their indexes) if they are absent. Silent.

    For tables added after a node was installed. `migrate` only runs from the setup
    scripts, and an upgrade that pulls code and restarts the service never reruns it --
    so a node would be running new code against an older schema. Everything here is
    `IF NOT EXISTS`, so this is a no-op on an up-to-date database.
    """
    for name in names:
        table_sql = TABLES.get(name)
        if not table_sql:
            continue
        cursor.execute(table_sql)
    for index_sql in INDEXES:
        # Only the indexes belonging to the tables asked for; the rest may not exist.
        if any(f" ON {name} " in index_sql for name in names):
            cursor.execute(index_sql)


def create_tables(cursor):
    """Create every table in the SQLite database."""
    for table_name, table_sql in TABLES.items():
        try:
            cursor.execute(table_sql)
            print(f"Created or updated '{table_name}' table.")
        except sqlite3.Error as e:
            print(f"Error creating '{table_name}' table: {e}")

    for index_sql in INDEXES:
        try:
            cursor.execute(index_sql)
        except sqlite3.Error as e:
            print(f"Error creating index: {e}")

    # Additive column migrations for databases created before a column existed.
    # `CREATE TABLE IF NOT EXISTS` never alters an existing table, so new columns
    # must be back-filled here. Each entry is idempotent (skipped when present).
    # `arch` is the guest's architecture, which selects the memory price when the
    # operator has set one per arch. NULL on every row written before this column
    # existed, and NULL is charged the node's scalar memory price, so an existing
    # database needs no back-fill.
    ensure_columns(cursor, "local_instances", {
        "envs": "TEXT DEFAULT NULL",
        "arch": "TEXT DEFAULT NULL",
    })
    ensure_columns(cursor, "peer", {
        "last_ts": "INTEGER DEFAULT NULL",
        "advertisement": "BLOB DEFAULT NULL",
    })
    # What a payment was *for*, as opposed to how far it got. A donation this node
    # paid out of its own earnings is not a payment to a peer, and `status` cannot say
    # so: it describes the lifecycle (communicated / accepted / rejected), which a
    # donation has just as much as any other payment. NULL is an ordinary payment,
    # which is every row written before this column existed.
    ensure_columns(cursor, "payments", {
        "purpose": "TEXT DEFAULT NULL",
        "token_id": "TEXT DEFAULT NULL",
    })
    ensure_columns(cursor, "uri", {
        "peer_id": "TEXT DEFAULT NULL",
        "expiry_unix_timestamp": "INTEGER DEFAULT NULL",
        "transport": "TEXT DEFAULT NULL",
        "protocol_stack": "BLOB DEFAULT NULL",
    })
    retire_slot_table(cursor)
    ensure_peer_address_uniqueness(cursor)
    widen_contract_instance_uniqueness(cursor)


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


def widen_contract_instance_uniqueness(cursor) -> None:
    """Make ``contract_instance`` unique per *asset*, rebuilding the table if it is not.

    A payment method is ledger + contract + asset, so the row that carries a method's
    rate has to be unique per asset. An older database declares
    ``UNIQUE (address, ledger_hash, contract_hash, peer_id)``, which SQLite cannot
    ALTER away -- and leaving it is not an option twice over: two assets on one contract
    would collide on insert, and `add_contract`'s ``ON CONFLICT`` names the five columns,
    so against the old constraint every peer registration would fail outright.

    The issue that added the asset dimension says no migration is provided, on the
    grounds that nodo is not in production. This goes further because the failure mode
    is not "the feature is missing" but "peer registration raises on every call", and
    the file already rebuilds a table when it has to (see `retire_slot_table`).

    Idempotent: a no-op once the constraint is the wide one.
    """
    try:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='contract_instance'"
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return
        declared = " ".join(str(row[0]).split())
        if "token_id" in declared and "contract_hash, token_id, peer_id" in declared:
            return

        print("Rebuilding 'contract_instance' so a payment method is unique per asset.")
        cursor.execute("PRAGMA table_info(contract_instance)")
        columns = [info[1] for info in cursor.fetchall()]
        has_token = "token_id" in columns

        cursor.execute("ALTER TABLE contract_instance RENAME TO contract_instance_old")
        cursor.execute(TABLES["contract_instance"])
        # An older row carries no asset. It is the chain's native unit by construction:
        # that is the only thing anything could have been advertising before the
        # dimension existed. Left empty rather than guessed at a symbol, because the
        # contract's own `NATIVE_ASSET` is what names it and this file knows no ledgers.
        token_column = "token_id" if has_token else "''"
        cursor.execute(
            "INSERT OR IGNORE INTO contract_instance "
            "(address, ledger_hash, contract_hash, token_id, peer_id, mu_per_unit) "
            f"SELECT address, ledger_hash, contract_hash, {token_column}, peer_id, "
            "mu_per_unit FROM contract_instance_old"
        )
        cursor.execute("DROP TABLE contract_instance_old")
    except sqlite3.Error as e:
        print(f"Error widening the contract_instance uniqueness: {e}")


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
