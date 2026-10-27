"""``nodo receipt``: check execution receipts (#419, docs/EXECUTION_RECEIPTS.md).

    nodo receipt verify <file> [--token <instance token>] [--peer <peer_id>] [--json]
        Reads the receipts in <file> and checks each one and the set. Exits 0 only
        when every receipt is valid and the set has no error and no contradiction.

        <file> is JSON: one receipt, a list of receipts, an object with a "receipts"
        list, or one receipt per line. A receipt is the proto3 JSON mapping of
        ``celaut.ExecutionReceipt`` with the field names of celaut.proto, which is
        what ``nodo execute --receipt`` writes.

        --token  the instance token: each receipt must name this execution.
        --peer   the node: each receipt must be signed by this peer_id.

Read-only, and it needs nothing of the node: no database, no daemon, no network. A
client checks receipts of a node that it does not run, so the check uses only the
receipt and the public key in it (``execution_receipt.check_sequence``).
"""
import json
from typing import Dict, List, Optional

from src.commands import _catalogue as catalogue

USAGE = "Usage: nodo receipt verify <file> [--token <instance token>] [--peer <peer_id>] [--json]"


def _take(argv: List[str], flag: str) -> Optional[str]:
    if flag not in argv:
        return None
    index = argv.index(flag)
    if index + 1 >= len(argv):
        raise ValueError(f"{flag} needs a value")
    value = argv[index + 1]
    del argv[index:index + 2]
    return value


def read_receipts(path: str) -> List:
    """Every receipt in ``path``, in file order. See the module docstring for the shapes.

    Raises ``OSError`` for a file it cannot open, and ``ValueError`` for any content
    that is not receipts: bad JSON, an entry that is not an object, or an object that
    is not an ``ExecutionReceipt`` (an unknown field, a bad base64 value, a wrong type).
    A client runs this on files that it did not write, so no other error comes out.
    """
    from google.protobuf import json_format
    from src.identity.execution_receipt import from_json_dict

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(document, dict) and "receipts" in document:
        document = document["receipts"]
    if isinstance(document, dict):
        document = [document]
    if not isinstance(document, list):
        raise ValueError("the file is not a receipt, a list of receipts or one per line")
    receipts = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {index} is not a JSON object")
        try:
            receipts.append(from_json_dict(entry))
        except (json_format.ParseError, TypeError, AttributeError) as e:
            raise ValueError(f"entry {index} is not an ExecutionReceipt: {e}") from e
    return receipts


def write_receipt(path: str, receipt) -> None:
    """Append ``receipt`` to ``path`` as one JSON line, the shape ``verify`` reads."""
    from src.identity.execution_receipt import to_json_dict

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(to_json_dict(receipt), sort_keys=True) + "\n")


def _print_report(report: Dict) -> None:
    for entry in report["receipts"]:
        where = f"seq {entry['seq']} of {entry['session'][:8]}" if entry["session"] else "start"
        mark = "OK " if entry["valid"] else "BAD"
        print(f"  {mark} #{entry['index']} {entry['kind']:<10} {where}", flush=True)
        for problem in entry["problems"]:
            print(f"        - {problem}", flush=True)
    for session, stream in report["streams"].items():
        print(
            f"  stream {session[:8]} ({stream['plane']}, {stream['coverage']}): "
            f"last seq {stream['last_seq']}, {'closed' if stream['closed'] else 'NOT closed'}, "
            f"in {stream['in_events']} events/{stream['in_bytes']} B, "
            f"out {stream['out_events']} events/{stream['out_bytes']} B"
            + (f", charged {stream['mu_charged']} MU" if stream["mu_charged"] else ""),
            flush=True,
        )
    for gap in report["gaps"]:
        print(f"  gap in stream {gap['session'][:8]}: seq {gap['from']}..{gap['to']} missing",
              flush=True)
    for error in report["errors"]:
        print(f"  ERROR: {error}", flush=True)
    for contradiction in report["contradictions"]:
        print(
            f"  CONTRADICTION: two signed receipts for {contradiction['kind']} "
            f"seq {contradiction['seq']} with different content", flush=True,
        )
    print("Valid." if report["valid"] else "Not valid.", flush=True)


def verify_command(argv: List[str]) -> bool:
    from src.identity.execution_receipt import check_sequence

    argv = list(argv)
    as_json = "--json" in argv and not argv.remove("--json")
    try:
        token = _take(argv, "--token")
        peer_id = _take(argv, "--peer")
    except ValueError as e:
        return catalogue.emit_error(as_json, f"{e}. {USAGE}")
    if len(argv) != 1:
        return catalogue.emit_error(as_json, USAGE)
    try:
        receipts = read_receipts(argv[0])
    except (OSError, ValueError, RecursionError) as e:  # a JSON nested too deep: RecursionError
        return catalogue.emit_error(as_json, f"Cannot read receipts from {argv[0]}: {e}")
    if not receipts:
        return catalogue.emit_error(as_json, f"No receipt in {argv[0]}.")

    report = check_sequence(receipts, peer_id=peer_id, token=token)
    if as_json:
        catalogue.emit_json(report)
    else:
        _print_report(report)
    return report["valid"]


def receipt_command(argv: List[str]) -> bool:
    if argv and argv[0] == "verify":
        return verify_command(argv[1:])
    as_json = "--json" in argv
    return catalogue.emit_error(as_json, USAGE)
