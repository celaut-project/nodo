import sqlite3
from src.utils.config import ConfigManager
from src.utils.monetary import format_mu

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")


def _client_records(connection, client_id=None):
    """Every client row (or one), as plain data -- what both renderings read."""
    from src.commands._catalogue import column_exists, mu_or_none

    unmetered = "unmetered" if column_exists(connection, "clients", "unmetered") else "0"
    query = f"SELECT id, balance_mu, last_usage, {unmetered} FROM clients"
    params = ()
    if client_id is not None:
        query += " WHERE id = ?"
        params = (client_id,)
    records = []
    for id_, balance_mu, last_usage, is_unmetered in connection.execute(query, params).fetchall():
        balance = mu_or_none(balance_mu)
        records.append({
            "id": id_,
            "balance_mu": balance,
            "balance_display": format_mu(balance) if balance is not None else None,
            "last_usage": last_usage,
            "unmetered": bool(is_unmetered),
        })
    return records


def _client_detail(connection, client_id, limit):
    """What the TUI's CLIENTS detail card adds (clients.rs `get_client_detail`):
    the deposit tokens it holds, the instances it started here, what it paid us,
    and which peer (if any) it is."""
    from src.commands import _catalogue as catalogue

    connection.row_factory = sqlite3.Row
    detail = {"deposit_tokens": [], "instances": [], "bound_peer_id": None}
    if catalogue.table_exists(connection, "deposit_tokens"):
        detail["deposit_tokens"] = [dict(row) for row in connection.execute(
            "SELECT id, status, created_at FROM deposit_tokens "
            "WHERE client_id = ? ORDER BY created_at DESC LIMIT ?", (client_id, limit))]
    if catalogue.table_exists(connection, "local_instances"):
        # `father_id` holds the client id for a top-level instance: the only link
        # between a client and anything it runs.
        detail["instances"] = [dict(row) for row in connection.execute(
            "SELECT id, COALESCE(name, '') AS name FROM local_instances "
            "WHERE father_id = ? ORDER BY name LIMIT ?", (client_id, limit))]
    if catalogue.table_exists(connection, "peer") and \
            catalogue.column_exists(connection, "peer", "local_client_id"):
        row = connection.execute(
            "SELECT id FROM peer WHERE local_client_id = ?", (client_id,)).fetchone()
        detail["bound_peer_id"] = row[0] if row else None
    detail["payments"] = catalogue.payments(connection, "client_id", client_id, limit)
    return detail


def clients_command(argv=None) -> bool:
    """``nodo clients [<client_id>] [--json] [--limit N]``.

    No id lists every client, as it always has. An id narrows to one and adds the
    TUI's CLIENTS detail card: payments, deposit tokens, instances, bound peer.
    """
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
    as_json = "--json" in argv
    try:
        limit = catalogue.take_limit(argv)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    args = catalogue.positionals(argv)
    client_id = args[0] if args else None

    connection = sqlite3.connect(DATABASE_FILE)
    try:
        if not catalogue.table_exists(connection, "clients"):
            if as_json:
                catalogue.emit_json({"clients": []})
            else:
                print("Warning: The 'clients' table does not exist in the database.")
            return client_id is None
        records = _client_records(connection, client_id)
        if client_id is not None:
            if not records:
                return catalogue.emit_error(as_json, f"No client with id {client_id}.")
            records[0].update(_client_detail(connection, client_id, limit))
    except sqlite3.Error as e:
        return catalogue.emit_error(as_json, f"An error occurred while listing clients: {e}")
    finally:
        connection.close()

    if as_json:
        catalogue.emit_json({"client": records[0]} if client_id is not None else {"clients": records})
        return True
    if not records:
        print("No clients found.")
        return True
    for record in records:
        # Section: General
        print(f"ID: {record['id']}")

        # Section: Balance & Usage
        print("[Balance & Usage]")
        print(f"  Balance: {record['balance_display'] or 'Invalid balance'}")
        print(f"  Last Usage: {record['last_usage'] if record['last_usage'] is not None else 'None'}")
        if record["unmetered"]:
            print("  Metering: unmetered")
        if client_id is not None:
            print(f"  Bound peer: {record['bound_peer_id'] or 'None'}")
            print(f"[Payments] ({len(record['payments'])}, newest first)")
            for payment in record["payments"]:
                amount = catalogue.mu_or_none(payment["amount_mu"])
                print(f"  {payment['created_at']}  {payment['direction']:<3} "
                      f"{format_mu(amount) if amount is not None else payment['amount_mu']}  "
                      f"{payment['status']}  {payment['deposit_token'] or '-'}")
            print(f"[Deposit tokens] ({len(record['deposit_tokens'])})")
            for token in record["deposit_tokens"]:
                print(f"  {token['id']}  {token['status']}  {token['created_at']}")
            print(f"[Instances] ({len(record['instances'])})")
            for instance in record["instances"]:
                print(f"  {instance['id']}  {instance['name']}")
        print()

        print("-" * 40 + "\n")
    return True


def list_clients():
    """Print every client stored in the database (``nodo clients``)."""
    clients_command([])
