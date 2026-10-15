import sqlite3

from src.utils.config import ConfigManager
from src.utils import keyvalue
from protos import celaut_pb2 as celaut
from src.utils.logger import ssformat
from src.utils.monetary import format_mu
from src.utils.contract_xattrs import get_token_id
from src.database.sql_connection import SQLConnection


def _balance_in_local_mu(peer_id: str, balance_str) -> "int | None":
    """The stored peer-MU balance expressed in ours, or None if it cannot be.

    `None` rather than 0, because this is a display: an operator has to be able to
    tell "you have nothing there" apart from "we cannot say what their MU is worth".
    """
    from src.payment_system.mu_conversion import convert_mu, matching_payment_system

    try:
        payment_system = matching_payment_system(peer_id)
        return convert_mu(
            int(balance_str),
            from_mu_per_unit=payment_system.peer_mu_per_unit,
            to_mu_per_unit=payment_system.local_mu_per_unit,
        )
    except (ValueError, TypeError):
        return None

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")

sq = SQLConnection()

def _peer_records(connection, peer_id=None):
    """Every peer row (or one), decoded into plain data.

    The one reading of the ``peer`` table that both the printed listing and
    ``--json`` render, so the two cannot disagree about a peer.
    """
    query = (
        "SELECT id, advertisement, remote_client_id, local_client_id, "
        "balance_mu, balance_last_update, reputation_score, "
        "reputation_index, last_index_on_ledger FROM peer"
    )
    params = ()
    if peer_id is not None:
        query += " WHERE id = ?"
        params = (peer_id,)
    has_uri = _table_exists(connection, "uri")
    records = []
    for (
        peer_id_, advertisement, remote_client_id, local_client_id,
        balance_str, balance_last_update, reputation_score,
        reputation_index, last_index_on_ledger,
    ) in connection.execute(query, params).fetchall():
        advertised_rates = {}
        proof_ids = []
        protocol_stack = []
        advertisement_error = None
        if advertisement:
            announced = celaut.Peer()
            try:
                announced.ParseFromString(advertisement)
            except Exception as e:
                advertisement_error = str(e)
                announced = celaut.Peer()
            # The stack is per-address now, so show the union across the peer's
            # addresses rather than a single gateway slot's.
            protocol_stack = sorted({
                protocol.tags[0]
                for uri in announced.uri
                for protocol in uri.protocol_stack
                if protocol.tags
            })
            # Rates the peer advertised. Absent for peers running a version from
            # before nodes published them.
            advertised_rates = {
                rate: amount.n for rate, amount in keyvalue.items(announced.mu_per_call)
            }
            # Every proof the peer announced, not just one: a node can hold
            # several, and each is an opinion set it published (issue #281).
            proof_ids = [
                token_id for token_id in (
                    get_token_id(contract) for contract in announced.reputation_proofs
                ) if token_id
            ]
        endpoints = []
        if has_uri:
            endpoints = [
                f"{ip}:{port}" for ip, port in connection.execute(
                    "SELECT ip, port FROM uri WHERE peer_id = ?", (peer_id_,)
                ).fetchall()
            ]
        # The column holds the peer's own MU (see
        # `SQLConnection.refresh_balance_for_peer`), and `format_mu` renders in
        # *our* display unit -- so it has to be converted before it is shown, or
        # the figure is a number in one currency labelled with another.
        balance = _balance_in_local_mu(peer_id_, balance_str)
        records.append({
            "id": peer_id_,
            "endpoints": endpoints,
            "protocol_stack": protocol_stack,
            "remote_client_id": remote_client_id,
            "local_client_id": local_client_id,
            "balance_peer_mu": balance_str,
            "balance_mu": balance,
            "balance_display": format_mu(balance) if balance is not None else None,
            "balance_last_update": balance_last_update,
            "payment_methods": sq.get_peer_payment_contracts(peer_id_),
            "advertised_rates": advertised_rates,
            "reputation_proofs": proof_ids,
            "reputation_score": reputation_score,
            "reputation_index": reputation_index,
            "last_index_on_ledger": last_index_on_ledger,
            "advertisement_error": advertisement_error,
        })
    return records


def _table_exists(connection, name: str) -> bool:
    return connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,)
    ).fetchone() is not None


# Decimals of the natives a person reads in whole units. A token's are not on the wire.
_NATIVE_DECIMALS = {"ERG": 9, "BTC": 8}
_NATIVE_OF_LEDGER = {"ergo": "ERG", "bitcoin": "BTC"}


def _rate_for_a_person(contract) -> str:
    """A peer's advertised rate as a person reads it.

    On the wire (``ContractRate.mu_per_unit``) the rate is MU per BASE unit -- nanoERG,
    satoshi, a token's smallest unit. A person thinks in whole ERG or BTC, so a native
    asset's rate is shown per whole unit too; a token's decimals are not on the wire,
    so its rate stays per base unit and says so. Same reading as the TUI's PEERS card.
    """
    rate = contract.get("mu_per_unit")
    if rate is None:
        return "N/A"
    asset = contract.get("token_id") or _NATIVE_OF_LEDGER.get(contract.get("ledger_tag"), "")
    decimals = _NATIVE_DECIMALS.get(asset)
    if decimals is None:
        return f"{rate} MU per base unit"
    return f"{rate} MU per base unit (1 {asset} = {int(rate) * 10 ** decimals} MU)"


def _print_peer(record) -> None:
    peer_id = record["id"]
    if record["advertisement_error"]:
        print(f"  (unreadable advertisement for {peer_id}: {record['advertisement_error']})")

    # Section: General
    print(f"ID: {peer_id}")
    print("[General]")
    print(f"  Protocol stack: {' '.join(record['protocol_stack']) or 'N/A'}")
    if record["endpoints"]:
        print(f"  Endpoints: {', '.join(record['endpoints'])}")
    print()

    # Section: Client & Balance
    print("[Client & Balance]")
    print(f"  Remote Client ID: {record['remote_client_id']}")
    print(
        f"  Our balance there: {record['balance_display']}" if record["balance_mu"] is not None
        else f"  Our balance there: {record['balance_peer_mu']} (the peer's own MU; no "
             "common payment system to convert it)"
    )
    print(f"  Balance last update: {record['balance_last_update'] or 'None'}")
    print()

    # Section: Payment methods
    # Every payment method this peer has registered, across every ledger,
    # contract type and asset -- not just a single hardcoded one. A method is
    # ledger + contract + asset, and the asset is the part that cannot be left
    # out: on Ergo one contract is paid in ERG and in every token at the same
    # address, so two rows here can differ in nothing else -- and each carries
    # its own rate.
    print("[Payment Methods]")
    if record["payment_methods"]:
        for contract in record["payment_methods"]:
            asset = contract.get('token_id') or 'native'
            print(f"  Ledger: {contract['ledger_tag']}  Asset: {asset}")
            print(f"    Contract hash: {contract['contract_hash']}")
            print(f"    Address:       {contract['address'] or 'N/A'}")
            print(f"    Rate:          {_rate_for_a_person(contract)}")
    else:
        print("  No payment method registered for this peer.")
    print()

    # Section: Advertised rates
    # What this peer charges on a recurring basis, as it advertised. These
    # are base prices, not quotes -- the price of a specific service still
    # comes from GetServiceEstimatedCost.
    print("[Rates] (base prices in MU; see [Contracts] for what an MU is worth)")
    if record["advertised_rates"]:
        for rate, value in sorted(record["advertised_rates"].items()):
            print(f"  {rate}: {value}")
    else:
        print("  Not advertised by this peer.")
    print()

    # Section: Reputation
    # Score/index are our own first-hand opinion of this peer, keyed by its
    # public key. The proof ids are the peer's opinions about others -- what
    # it announced, unverified here (run `nodo verify_reputation <peer>`).
    print("[Reputation]")
    if record["reputation_proofs"]:
        print(f"  Announced proof IDs ({len(record['reputation_proofs'])}):")
        for token_id in record["reputation_proofs"]:
            print(f"    {token_id}")
    else:
        print("  Announced proof IDs: None")
    print(f"  Score: {record['reputation_score'] or 'None'}")
    print(f"  Index: {record['reputation_index'] or 'None'}")
    print(f"  Last Index on Ledger: {record['last_index_on_ledger'] or 'None'}")


def _print_history(record) -> None:
    """The detail card's history: what we paid this peer, and why its score moved."""
    print()
    print(f"[Payments] ({len(record['payments'])}, newest first)")
    for payment in record["payments"]:
        amount = format_mu(int(payment["amount_mu"])) if str(payment["amount_mu"]).isdigit() \
            else payment["amount_mu"]
        print(f"  {payment['created_at']}  {payment['direction']:<3} {amount:>14}  "
              f"{payment['status']}  {payment['tx_id'] or '-'}")
    if not record["payments"]:
        print("  None.")
    print(f"[Reputation events] ({len(record['reputation_events'])}, newest first)")
    for event in record["reputation_events"]:
        amount = f"{event['amount']:+d}" if isinstance(event["amount"], int) else event["amount"]
        print(f"  {event['created_at']}  {amount}  -> {event['score_after']}  "
              f"{event['reason']}")
    if not record["reputation_events"]:
        print("  None.")


def peers_command(argv=None) -> bool:
    """``nodo peers [<peer_id>] [--json] [--limit N]``.

    No id lists every peer, as it always has. An id narrows to that one peer and
    adds what the TUI's PEERS detail card adds: every payment made to it and the
    reputation events behind its score. ``--json`` prints the same as one object.
    """
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
    as_json = "--json" in argv
    try:
        limit = catalogue.take_limit(argv)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    args = catalogue.positionals(argv)
    peer_id = args[0] if args else None

    connection = sqlite3.connect(DATABASE_FILE)
    try:
        if not _table_exists(connection, "peer"):
            if as_json:
                catalogue.emit_json({"peers": []})
                return peer_id is None
            print("Warning: The 'peer' table does not exist in the database.")
            return peer_id is None
        records = _peer_records(connection, peer_id)
        if peer_id is not None and not records:
            return catalogue.emit_error(as_json, f"No peer with id {peer_id}.")
        if peer_id is not None:
            connection.row_factory = sqlite3.Row
            records[0]["payments"] = catalogue.payments(connection, "peer_id", peer_id, limit)
            records[0]["reputation_events"] = catalogue.reputation_events(
                connection, "peer", peer_id, limit)
    except sqlite3.Error as e:
        return catalogue.emit_error(as_json, f"An error occurred while listing peers: {e}")
    finally:
        connection.close()

    if as_json:
        catalogue.emit_json({"peer": records[0]} if peer_id is not None else {"peers": records})
        return True
    if not records:
        print("No peers found.")
        return True
    for record in records:
        _print_peer(record)
        if peer_id is not None:
            _print_history(record)
        print("-" * 40 + "\n")
    return True


def list_peers():
    """Print every peer stored in the database (``nodo peers``)."""
    peers_command([])


def adjust_peer_reputation(peer_id: str, delta: str, as_json: bool = False) -> bool:
    """``nodo peer_reputation <peer_id> <+N|-N>`` -- the TUI's ``+``/``-`` on PEERS.

    The same write the TUI makes (``adjust_peer_reputation`` in peers.rs): add
    ``delta`` to the local score, step the index, and record an
    ``operator_adjustment`` event in the same transaction, through the one
    Python mover of a peer's score so the history always adds up to it.
    """
    from src.commands import _catalogue as catalogue
    from src.reputation_system.reasons import Reason

    try:
        amount = int(str(delta))
    except ValueError:
        return catalogue.emit_error(as_json, f"Delta must be a whole number, e.g. +1 or -1 (got {delta!r}).")
    if amount == 0:
        return catalogue.emit_error(as_json, "Delta must not be zero.")
    if not sq.peer_exists(peer_id):
        return catalogue.emit_error(as_json, f"No peer with id {peer_id}.")
    if not sq.update_reputation_peer(peer_id, amount, Reason.OPERATOR_ADJUSTMENT):
        return catalogue.emit_error(as_json, f"Could not adjust the reputation of peer {peer_id}; see app.log.")
    connection = sqlite3.connect(DATABASE_FILE)
    try:
        row = connection.execute(
            "SELECT reputation_score, reputation_index FROM peer WHERE id = ?", (peer_id,)
        ).fetchone()
    finally:
        connection.close()
    score, index = (row or (None, None))
    if as_json:
        catalogue.emit_json({"peer_id": peer_id, "delta": amount,
                             "reputation_score": score, "reputation_index": index})
    else:
        print(f"Peer {peer_id}: reputation {amount:+d} -> score {score}.", flush=True)
    return True


def _refresh_one_peer(peer_id: str) -> dict:
    """Re-read one peer from the network: its announcement, then what we hold there.

    The two halves fail on their own: a peer whose ``GetPeerInfo`` is refused can
    still answer ``Metrics`` and the other way round, so each reports its own outcome.
    """
    from src.manager.manager import refresh_peer_instance
    from src.manager.metrics import refresh_balance_on_peer

    result = {"id": peer_id, "peer_refreshed": False, "balance_refreshed": False,
              "balance_peer_mu": None, "balance_mu": None, "error": None}
    try:
        result["peer_refreshed"] = bool(refresh_peer_instance(peer_id))
    except Exception as e:
        result["error"] = f"peer: {e}"
    try:
        balance = refresh_balance_on_peer(peer_id)
        result["balance_refreshed"] = True
        result["balance_peer_mu"] = str(balance)
        result["balance_mu"] = _balance_in_local_mu(peer_id, balance)
    except Exception as e:
        result["error"] = "; ".join(filter(None, [result["error"], f"balance: {e}"]))
    return result


def refresh_peers_command(argv=None) -> bool:
    """``nodo refresh_peers [<peer_id>] [--json]`` -- the TUI's ``r`` / ⟳ on PEERS.

    Re-fetches every known peer's ``Peer`` announcement (addresses, payment contracts,
    rates, reputation proofs) and our client's balance there, instead of waiting for the
    manager's next pass. With an id, only that peer. Succeeds when at least one peer
    was refreshed in full, or when there was nothing to refresh.
    """
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
    as_json = "--json" in argv
    args = catalogue.positionals(argv)
    if args:
        if not sq.peer_exists(args[0]):
            return catalogue.emit_error(as_json, f"No peer with id {args[0]}.")
        peer_ids = [args[0]]
    else:
        peer_ids = sq.get_peers_id()

    results = [_refresh_one_peer(peer_id) for peer_id in peer_ids]
    full = sum(1 for r in results if r["peer_refreshed"] and r["balance_refreshed"])
    if as_json:
        catalogue.emit_json({"peers": results, "refreshed": full, "total": len(results)})
    else:
        for r in results:
            state = "ok" if r["peer_refreshed"] and r["balance_refreshed"] else "partial" \
                if r["peer_refreshed"] or r["balance_refreshed"] else "failed"
            line = f"  {r['id']}: {state}"
            if r["balance_mu"] is not None:
                line += f", balance {format_mu(r['balance_mu'])}"
            if r["error"]:
                line += f" ({r['error']})"
            print(line)
        print(f"Refreshed {full} of {len(results)} peers.", flush=True)
    return full > 0 or not results
