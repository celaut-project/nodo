"""``nodo statement`` -- the balance statements of this node, as its operator reads them.

    nodo statement [--json] [--limit N]
        The accounts of this node: its clients, its instances and the instances that it
        delegated, with the balance, the head of the chain and the number of rows.
    nodo statement <account> [--from SEQ] [--limit N] [--kind KIND] [--json]
        The movements of one account, oldest first, and whether its chain holds together.
        ``<account>`` is a client_id, the name of an instance, or the start of an account
        reference (the hash that a statement shows, six characters at least).
    nodo statement faults [--limit N] [--json]
        The faults of peers that this node scored from their statements.
    nodo statement hops [--limit N] [--json]
        What this node keeps of each peer and descendant that it reached: the link, the head
        of the chain, the failures of the tunnel, and the last receipt of the peer.
    nodo statement check-receipts <receipts-file> <account> [--json]
        Check execution receipts against the rows of an account: the one check that the node
        uses on its peers (``src/identity/receipt_statement.py``).

A script and an agent read the same data with ``--json``: one object on one line. The
TUI reads it too (the LEDGER and RECEIPTS pages). Nothing here prints a token: an account
is named by its label and by its reference, the hash that a statement shows.

The rows come from this node's own database, so reading costs nothing. A statement of
another node's descendant is read by a service, with ``GetBalanceStatement``.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from src.commands import _catalogue as catalogue

USAGE = (
    "Usage: nodo statement [--json] [--limit N]\n"
    "       nodo statement <account> [--from SEQ] [--limit N] [--kind KIND] [--json]\n"
    "       nodo statement faults [--limit N] [--json]\n"
    "       nodo statement hops [--limit N] [--json]\n"
    "       nodo statement check-receipts <receipts-file> <account> [--json]"
)

DEFAULT_ACCOUNTS = 200
DEFAULT_MOVEMENTS = 100
MIN_REF_CHARS = 6


# ---------------------------------------------------------------------------------------
# The data
# ---------------------------------------------------------------------------------------

def _heads(connection) -> Dict[str, Dict[str, Any]]:
    """For each account with rows: its last seq and hash, its row count, its last date."""
    if not catalogue.table_exists(connection, "balance_movements"):
        return {}
    heads: Dict[str, Dict[str, Any]] = {}
    for row in connection.execute(
            "SELECT account_id, MAX(seq) AS head_seq, COUNT(*) AS rows, MAX(ts) AS last_ts "
            "FROM balance_movements GROUP BY account_id"):
        head_hash = connection.execute(
            "SELECT hash FROM balance_movements WHERE account_id = ? AND seq = ?",
            (row["account_id"], row["head_seq"])).fetchone()
        heads[row["account_id"]] = {
            "head_seq": int(row["head_seq"]), "head_hash": head_hash["hash"],
            "rows": int(row["rows"]), "last_ts": int(row["last_ts"])}
    return heads


def accounts(connection) -> List[Dict[str, Any]]:
    """Every account of this node: a client, an instance, or an instance that it delegated.

    Alive ones first. ``ref`` is the account reference that a statement shows, and
    ``father_ref`` the one of its father. The id of an instance (its token) is not in the
    answer.
    """
    from src.database import ledger

    heads = _heads(connection)
    found: List[Dict[str, Any]] = []

    def add(kind, account_id, label, alive, balance, service_id="", father_id=None, peer_id=""):
        head = heads.get(account_id, {})
        found.append({
            "kind": kind, "ref": ledger.account_ref(account_id), "label": label,
            "alive": alive, "balance_mu": str(balance if balance is not None else 0),
            "service_id": service_id or "",
            "father_ref": ledger.account_ref(father_id) if father_id else "",
            "peer_id": peer_id or "",
            "head_seq": head.get("head_seq", 0), "head_hash": head.get("head_hash", ""),
            "rows": head.get("rows", 0), "last_ts": head.get("last_ts", 0),
            "_id": account_id,
        })

    if catalogue.table_exists(connection, "clients"):
        for row in connection.execute("SELECT id, balance_mu FROM clients"):
            add("client", row["id"], row["id"], True, row["balance_mu"])
    if catalogue.table_exists(connection, "local_instances"):
        for row in connection.execute(
                "SELECT id, name, service_id, father_id, killed_at, balance_mu "
                "FROM local_instances"):
            add("local", row["id"], row["name"], row["killed_at"] is None, row["balance_mu"],
                row["service_id"], row["father_id"])
    if catalogue.table_exists(connection, "delegated_instances"):
        for row in connection.execute(
                "SELECT id, peer_id, service_id, father_id, killed_at, balance_mu "
                "FROM delegated_instances"):
            add("delegated", row["id"], f"{row['id'][:8]}@{(row['peer_id'] or '')[:8]}",
                row["killed_at"] is None, row["balance_mu"], row["service_id"],
                row["father_id"], row["peer_id"])
    found.sort(key=lambda a: (not a["alive"], a["kind"], a["label"]))
    return found


def public(account: Dict[str, Any]) -> Dict[str, Any]:
    """The account without its private id."""
    return {k: v for k, v in account.items() if not k.startswith("_")}


class Ambiguous(Exception):
    pass


def resolve(connection, text: str) -> Optional[Dict[str, Any]]:
    """The account that ``text`` names: a client_id or a token (exactly), a label, or the
    start of a reference. None if none does. ``Ambiguous`` if more than one does."""
    every = accounts(connection)
    exact = [a for a in every if text in (a["_id"], a["label"], a["ref"])]
    if len({a["_id"] for a in exact}) == 1:
        return exact[0]
    if exact:
        raise Ambiguous(f"{text!r} names more than one account: use its reference.")
    if len(text) >= MIN_REF_CHARS:
        by_ref = [a for a in every if a["ref"].startswith(text.lower())]
        if len(by_ref) == 1:
            return by_ref[0]
        if by_ref:
            raise Ambiguous(f"{text!r} starts the reference of more than one account.")
    return None


def _decode(detail) -> Dict[str, Any]:
    if not detail:
        return {}
    try:
        value = json.loads(detail)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def movements(connection, account_id: str, from_seq: int = 1, limit: int = DEFAULT_MOVEMENTS,
              kind: Optional[str] = None) -> Tuple[List[Dict[str, Any]], bool]:
    """The rows of one account from ``from_seq`` on, oldest first, with ``kind`` if given.
    The second value says that more rows follow."""
    if not catalogue.table_exists(connection, "balance_movements"):
        return [], False
    sql = "SELECT * FROM balance_movements WHERE account_id = ? AND seq >= ?"
    params: List[Any] = [account_id, int(from_seq)]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY seq LIMIT ?"
    params.append(int(limit) + 1)
    rows = connection.execute(sql, params).fetchall()
    more = len(rows) > limit
    return [{
        "seq": int(r["seq"]), "ts": int(r["ts"]), "kind": r["kind"],
        "amount_mu": r["amount_mu"], "balance_after_mu": r["balance_after_mu"],
        "instance_ref": r["instance_ref"] or "", "detail": _decode(r["detail"]),
        "hash": r["hash"], "prev_hash": r["prev_hash"],
    } for r in rows[:limit]], more


def verify_chain(connection, account_id: str) -> Dict[str, Any]:
    """Whether the rows of an account hold together: each one hangs from the one before,
    its hash is the hash of its fields, and the balance adds up. A chain that starts after
    its first row (the retention deleted it) is ``partial``: it is checked from the first
    row that is held."""
    from src.database import ledger

    result = {"ok": True, "problem": "", "seq": 0, "checked": 0, "partial": False}
    if not catalogue.table_exists(connection, "balance_movements"):
        return result
    previous = None
    for row in connection.execute(
            "SELECT * FROM balance_movements WHERE account_id = ? ORDER BY seq", (account_id,)):
        seq = int(row["seq"])
        if previous is None:
            if seq == 1:
                if row["prev_hash"] != ledger.genesis_hash(account_id):
                    return {**result, "ok": False, "seq": seq,
                            "problem": "the first row does not hang from the genesis"}
                if int(row["balance_after_mu"]) != int(row["amount_mu"]):
                    return {**result, "ok": False, "seq": seq,
                            "problem": "the first row does not start from 0"}
            else:
                result["partial"] = True
        else:
            if seq != previous["seq"] + 1:
                return {**result, "ok": False, "seq": seq,
                        "problem": f"rows {previous['seq'] + 1} to {seq - 1} are missing"}
            if row["prev_hash"] != previous["hash"]:
                return {**result, "ok": False, "seq": seq,
                        "problem": "the row does not hang from the one before it"}
            if previous["balance"] + int(row["amount_mu"]) != int(row["balance_after_mu"]):
                return {**result, "ok": False, "seq": seq,
                        "problem": "the balance does not add up"}
        text = ledger.movement_hash(
            row["prev_hash"], seq, int(row["ts"]), row["amount_mu"], row["balance_after_mu"],
            row["kind"], row["instance_ref"], row["detail"])
        if text != row["hash"]:
            return {**result, "ok": False, "seq": seq,
                    "problem": "the hash is not the hash of the fields of the row"}
        previous = {"seq": seq, "hash": row["hash"], "balance": int(row["balance_after_mu"])}
        result["checked"] += 1
    return result


def faults(connection, limit: int = DEFAULT_ACCOUNTS) -> List[Dict[str, Any]]:
    """The faults of peers that this node scored from their statements, newest first."""
    if not catalogue.table_exists(connection, "statement_faults"):
        return []
    return [{"ts": int(r["ts"]), "peer_id": r["peer_id"], "account_ref": r["account_ref"],
             "reason": r["reason"], "seq": int(r["seq"])}
            for r in connection.execute(
                "SELECT * FROM statement_faults ORDER BY ts DESC, seq DESC LIMIT ?", (limit,))]


def _receipt_view(blob, peer_id: str) -> Optional[Dict[str, Any]]:
    """What a stored receipt says, and whether it is a valid one of ``peer_id``."""
    if not blob:
        return None
    from protos import celaut_pb2
    from src.identity import execution_receipt

    receipt = celaut_pb2.ExecutionReceipt()
    try:
        receipt.ParseFromString(bytes(blob))
    except Exception:
        return {"kind": "", "valid": False, "problems": ["the stored receipt cannot be read"]}
    problems = execution_receipt.problems(receipt, peer_id=peer_id)
    return {
        "kind": receipt.kind, "plane": receipt.plane, "session": receipt.session,
        "seq": int(receipt.seq), "ts": int(receipt.ts),
        "mu_charged": receipt.mu_charged.n or "",
        "in_bytes": int(receipt.in_bytes), "out_bytes": int(receipt.out_bytes),
        "digest": execution_receipt.receipt_digest(receipt),
        "valid": not problems, "problems": problems,
    }


def hops(connection, limit: int = DEFAULT_ACCOUNTS) -> List[Dict[str, Any]]:
    """What this node keeps of each (peer, descendant) pair that it reached, newest first."""
    if not catalogue.table_exists(connection, "descendant_peers"):
        return []
    found = []
    for r in connection.execute(
            "SELECT * FROM descendant_peers ORDER BY COALESCE(last_ok_ts, linked_ts, "
            "failed_at, head_ts, 0) DESC LIMIT ?", (limit,)):
        row = dict(r)
        found.append({
            "peer_id": row["peer_id"], "instance_ref": row["instance_ref"],
            "linked_ts": row.get("linked_ts"), "first_linked_ts": row.get("first_linked_ts"),
            "head_seq": row.get("head_seq"), "head_ts": row.get("head_ts"),
            "last_ok_ts": row.get("last_ok_ts"), "failed_at": row.get("failed_at"),
            "last_failed_at": row.get("last_failed_at"),
            "receipt": _receipt_view(row.get("receipt"), row["peer_id"]),
        })
    return found


class _Row:
    """A row of the database as ``receipt_statement.check`` reads it."""
    __slots__ = ("seq", "ts", "amount", "kind", "detail")

    def __init__(self, seq, ts, amount, kind, detail):
        self.seq, self.ts, self.amount, self.kind, self.detail = seq, ts, amount, kind, detail


def rows_for_check(connection, account_id: str) -> List[_Row]:
    """The rows of an account, as ``receipt_statement.check`` reads them."""
    if not catalogue.table_exists(connection, "balance_movements"):
        return []
    return [_Row(int(r["seq"]), int(r["ts"]), int(r["amount_mu"]), r["kind"],
                 _decode(r["detail"]))
            for r in connection.execute(
                "SELECT * FROM balance_movements WHERE account_id = ? ORDER BY seq",
                (account_id,))]


def check_receipts(connection, account_id: str, receipts, peer_id: Optional[str] = None
                   ) -> Dict[str, Any]:
    """``receipt_statement.check`` of ``receipts`` against the rows of an account."""
    from src.identity import receipt_statement
    return receipt_statement.check(receipts, rows_for_check(connection, account_id),
                                   peer_id=peer_id)


# ---------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------

def _open():
    from src.utils.config import ConfigManager
    return catalogue.connect(ConfigManager().get("DATABASE_FILE"))


def _take(argv: List[str], flag: str) -> Optional[str]:
    """Remove ``flag value`` (or ``flag=value``) from ``argv`` and return the value."""
    for index, argument in enumerate(argv):
        if argument == flag:
            if index + 1 >= len(argv):
                raise ValueError(f"{flag} needs a value.")
            value = argv[index + 1]
            del argv[index:index + 2]
            return value
        if argument.startswith(flag + "="):
            del argv[index]
            return argument.split("=", 1)[1]
    return None


def _take_int(argv: List[str], flag: str, default: int, minimum: int) -> int:
    text = _take(argv, flag)
    if text is None:
        return default
    value = int(text)
    if value < minimum:
        raise ValueError(f"{flag} must be at least {minimum}.")
    return value


def _when(ts) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts))) if ts else "-"


def _print_accounts(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No account on this node.", flush=True)
        return
    print(f"{'REF':14} {'KIND':10} {'LABEL':24} {'ST':5} {'BALANCE':>12} {'HEAD':>6}  SERVICE",
          flush=True)
    for a in rows:
        print(f"{a['ref'][:12]:14} {a['kind']:10} {a['label'][:24]:24} "
              f"{'live' if a['alive'] else 'dead':5} {a['balance_mu']:>12} "
              f"{a['head_seq']:>6}  {a['service_id'][:12]}", flush=True)


def _print_movements(document: Dict[str, Any]) -> None:
    account, chain = document["account"], document["chain"]
    print(f"{account['label']} ({account['kind']}, {'live' if account['alive'] else 'dead'}) "
          f"ref {account['ref'][:12]}  balance {account['balance_mu']} MU  "
          f"head {account['head_seq']}", flush=True)
    state = "holds together" if chain["ok"] else f"BROKEN at {chain['seq']}: {chain['problem']}"
    partial = " (checked from the first row that is held)" if chain["partial"] else ""
    print(f"Chain: {state}{partial}. {chain['checked']} rows checked.", flush=True)
    print(f"{'SEQ':>5} {'WHEN':19} {'KIND':14} {'AMOUNT':>12} {'BALANCE':>12}  DETAIL", flush=True)
    for m in document["movements"]:
        detail = " ".join(f"{k}={str(v)[:24]}" for k, v in m["detail"].items())
        print(f"{m['seq']:>5} {_when(m['ts']):19} {m['kind']:14} {m['amount_mu']:>12} "
              f"{m['balance_after_mu']:>12}  {detail}", flush=True)
    if document["has_more"]:
        print(f"More rows follow: --from {document['movements'][-1]['seq'] + 1}.", flush=True)


def print_check(report: Dict[str, Any]) -> None:
    """The text form of a ``receipt_statement.check`` report."""
    print(f"Receipts checked against the rows: {report['checked']}.", flush=True)
    for finding in report["findings"]:
        where = f"row {finding['seq']}" if finding["seq"] else "the receipt"
        print(f"  CONTRADICTION ({finding['kind']}, {where}): {finding['detail']}", flush=True)
    for reason in report["skipped"]:
        print(f"  not judged: {reason}", flush=True)
    print("Agree." if report["valid"] else "Do not agree.", flush=True)


def statement_command(argv: List[str]) -> bool:
    argv = list(argv)
    as_json = "--json" in argv and not argv.remove("--json")
    try:
        if argv and argv[0] == "faults":
            limit = _take_int(argv[1:], "--limit", DEFAULT_ACCOUNTS, 1) if len(argv) > 1 \
                else DEFAULT_ACCOUNTS
            return _faults(limit, as_json)
        if argv and argv[0] == "hops":
            limit = _take_int(argv[1:], "--limit", DEFAULT_ACCOUNTS, 1) if len(argv) > 1 \
                else DEFAULT_ACCOUNTS
            return _hops(limit, as_json)
        if argv and argv[0] == "check-receipts":
            return _check_command(argv[1:], as_json)
        from_seq = _take_int(argv, "--from", 1, 1)
        limit = _take_int(argv, "--limit", DEFAULT_MOVEMENTS if any(
            not a.startswith("-") for a in argv) else DEFAULT_ACCOUNTS, 1)
        kind = _take(argv, "--kind")
    except ValueError as e:
        return catalogue.emit_error(as_json, f"{e} {USAGE}")
    unknown = [a for a in argv if a.startswith("-")]
    names = [a for a in argv if not a.startswith("-")]
    if unknown or len(names) > 1:
        return catalogue.emit_error(as_json, USAGE)
    connection = _open()
    try:
        if not names:
            rows = [public(a) for a in accounts(connection)][:limit]
            if as_json:
                catalogue.emit_json({"accounts": rows})
            else:
                _print_accounts(rows)
            return True
        try:
            account = resolve(connection, names[0])
        except Ambiguous as e:
            return catalogue.emit_error(as_json, str(e))
        if account is None:
            return catalogue.emit_error(as_json, f"No account {names[0]!r} on this node.")
        rows, more = movements(connection, account["_id"], from_seq, limit, kind)
        document = {"account": public(account), "chain": verify_chain(connection, account["_id"]),
                    "movements": rows, "has_more": more}
        if as_json:
            catalogue.emit_json(document)
        else:
            _print_movements(document)
        return document["chain"]["ok"]
    finally:
        connection.close()


def _faults(limit: int, as_json: bool) -> bool:
    connection = _open()
    try:
        rows = faults(connection, limit)
    finally:
        connection.close()
    if as_json:
        catalogue.emit_json({"faults": rows})
        return True
    if not rows:
        print("No fault of a peer was scored from its statements.", flush=True)
        return True
    print(f"{'WHEN':19} {'PEER':14} {'REASON':32} {'SEQ':>5}  ACCOUNT", flush=True)
    for f in rows:
        print(f"{_when(f['ts']):19} {f['peer_id'][:12]:14} {f['reason']:32} {f['seq']:>5}  "
              f"{f['account_ref'][:12]}", flush=True)
    return True


def _hops(limit: int, as_json: bool) -> bool:
    connection = _open()
    try:
        rows = hops(connection, limit)
    finally:
        connection.close()
    if as_json:
        catalogue.emit_json({"hops": rows})
        return True
    if not rows:
        print("No peer reached for a descendant yet.", flush=True)
        return True
    for h in rows:
        receipt = h["receipt"]
        shown = ("no receipt" if receipt is None else
                 f"{receipt['kind']} {receipt['session'][:8]} {receipt['mu_charged'] or '-'} MU "
                 f"{'valid' if receipt['valid'] else 'NOT VALID'}")
        print(f"{h['peer_id'][:12]} {h['instance_ref'][:12]} linked {_when(h['linked_ts'])} "
              f"head {h['head_seq'] if h['head_seq'] is not None else '-'}  {shown}", flush=True)
    return True


def _check_command(argv: List[str], as_json: bool) -> bool:
    from src.commands.receipt import read_receipts

    names = [a for a in argv if not a.startswith("-")]
    if len(names) != 2:
        return catalogue.emit_error(as_json, USAGE)
    try:
        receipts = read_receipts(names[0])
    except (OSError, ValueError, RecursionError) as e:
        return catalogue.emit_error(as_json, f"Cannot read receipts from {names[0]}: {e}")
    if not receipts:
        return catalogue.emit_error(as_json, f"No receipt in {names[0]}.")
    connection = _open()
    try:
        try:
            account = resolve(connection, names[1])
        except Ambiguous as e:
            return catalogue.emit_error(as_json, str(e))
        if account is None:
            return catalogue.emit_error(as_json, f"No account {names[1]!r} on this node.")
        report = check_receipts(connection, account["_id"], receipts)
    finally:
        connection.close()
    if as_json:
        catalogue.emit_json(report)
    else:
        print_check(report)
    return report["valid"]
