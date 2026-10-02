import os
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager
from bee_rpc.utils import getsize
from protos.celaut_pb2 import Metadata

env_manager = ConfigManager()
REGISTRY = env_manager.get("REGISTRY")
METADATA = env_manager.get("METADATA_REGISTRY")

def format_bytes(size: int) -> str:
    """A byte count as ``nodo tui`` prints it: binary units, one decimal.

    The same function as ``format_bytes`` in src/commands/tui/src/app.rs, so the
    two views of one service read identically (issue #438). They used to disagree
    in how they said it: this printed MiB labelled "MB", always in MB, so a 394-byte
    directory read "0.00 MB" here and "394 B" in the TUI.
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{size} B" if unit == 0 else f"{value:.1f} {units[unit]}"


def _service_record(service: str) -> dict:
    """One registry entry as plain data: id, tag, and what it weighs."""
    # Initialize metadata for each service
    metadata = Metadata()

    # Try got get the tag
    try:  # TODO check. Repeated on instances.get_tag function.
        # Attempt to parse the metadata from the binary file
        with open(os.path.join(METADATA, service), "rb") as f:
            metadata.ParseFromString(f.read())
        name = metadata.hashtag.tag[0] if metadata.hashtag.tag else ""
    except FileNotFoundError:
        name = ""
    except Exception:
        name = ""

    # What the service weighs, blocks included.
    #
    # `getsize` already totals both halves -- the parts stored in the service's
    # own directory, and the content-addressed blocks it references -- so the
    # old TODO here was already satisfied by the call it was written above.
    # What was missing is that the two are different questions: the blocks are
    # shared with every other service referencing the same bytes, so the total
    # is what this service *is*, and the directory is what it *adds* to the
    # disk. Both are printed, because a service that stores 64 bytes and weighs
    # 8 GiB is neither figure on its own.
    record = {"id": service, "tag": name, "size_bytes": None, "stored_bytes": None, "size_error": None}
    try:
        record["size_bytes"] = getsize(os.path.join(REGISTRY, service))
        record["stored_bytes"] = sum(
            os.path.getsize(os.path.join(dirpath, file_name))
            for dirpath, _, names in os.walk(os.path.join(REGISTRY, service))
            for file_name in names
        )
    except Exception as e:
        record["size_error"] = str(e)
    return record


def _service_reputation(service_id: str, limit: int) -> dict:
    """The score and events behind it (``get_service_detail`` in the TUI).

    Scored by service id, so this is every instance of it that ever ran here.
    """
    from src.commands import _catalogue as catalogue

    connection = catalogue.connect(env_manager.get("DATABASE_FILE"))
    try:
        score = None
        if catalogue.table_exists(connection, "service_reputation"):
            row = connection.execute(
                "SELECT reputation_score FROM service_reputation WHERE service_id = ?",
                (service_id,)).fetchone()
            score = row[0] if row else None
        events = catalogue.reputation_events(connection, "service", service_id, limit)
    finally:
        connection.close()
    return {"reputation_score": score, "reputation_events": events}


def _size_text(record: dict) -> str:
    if record["size_error"] is not None:
        return f"0 - {record['size_error']}"
    # Worded as the TUI's two columns are ("With blocks", "Stored here").
    return (f"{format_bytes(record['size_bytes'])} with blocks "
            f"({format_bytes(record['stored_bytes'])} stored here)")


def services_command(argv=None) -> bool:
    """``nodo services [<service>] [--json] [--limit N]``.

    No argument lists the registry, as it always has. A service id or tag narrows
    to that one and adds the TUI's SERVICES detail card: its reputation score here
    and the events behind it.
    """
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
    as_json = "--json" in argv
    try:
        limit = catalogue.take_limit(argv)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    args = catalogue.positionals(argv)

    if args:
        try:
            service_id = get_id(args[0])
        except Exception as e:
            return catalogue.emit_error(as_json, f"Unknown service {args[0]}: {e}")
        if not service_id or not os.path.exists(os.path.join(REGISTRY, service_id)):
            return catalogue.emit_error(as_json, f"Service {args[0]} is not in the registry.")
        record = _service_record(service_id)
        record.update(_service_reputation(service_id, limit))
        if as_json:
            catalogue.emit_json({"service": record})
            return True
        print(f"ID: {record['id']}")
        print(f"Tag: {record['tag'] or '-'}")
        print(f"Size: {_size_text(record)}")
        print(f"Reputation score: {record['reputation_score'] if record['reputation_score'] is not None else 'None'}")
        print(f"[Reputation events] ({len(record['reputation_events'])}, newest first)")
        for event in record["reputation_events"]:
            print(f"  {event['created_at']}  {event['amount']}  -> {event['score_after']}  {event['reason']}")
        return True

    records = [_service_record(service) for service in os.listdir(REGISTRY)]
    if as_json:
        catalogue.emit_json({"services": records})
        return True
    for record in records:
        print(f"{record['id']}  {_size_text(record)} {record['tag']}")
    return True


def list_services():
    # List available services in the specified registry path
    services_command([])

def modify_tag(service: str, tag: str):
    service = get_id(service)
    
    metadata = Metadata()
    
    # Path to the metadata file for this service
    metadata_path = os.path.join(METADATA, service)
    
    # Try to load existing metadata if it exists
    try:
        with open(metadata_path, "rb") as f:
            metadata.ParseFromString(f.read())
    except FileNotFoundError:
        # If file doesn't exist, we'll create new metadata with just this tag
        metadata.hashtag.tag.append(tag)
    except Exception as e:
        print(f"Error reading metadata: {e}")
        return

    # Modify only the first tag
    if metadata.hashtag.tag:  # If there are existing tags
        metadata.hashtag.tag[0] = tag  # Replace the first one
    else:
        metadata.hashtag.tag.append(tag)  # Add as first tag if none exist
    
    # Save the updated metadata back to file
    try:
        with open(metadata_path, "wb") as f:
            f.write(metadata.SerializeToString())
        print(f"Successfully updated first tag for {service} to '{tag}'")
    except Exception as e:
        print(f"Error saving metadata: {e}")