"""``nodo get <service_id>`` -- ask the network for a service this node does not hold.

Two modes:

* Queued (default). The id is dropped as a marker file in
  `maintain.WANTED_INBOX_DIR`, which the running daemon's manager thread drains into
  `add_wanted` on its next short interval (`maintain.drain_wanted_inbox`) -- the same
  set `abstract_input_service_iterable.py` already feeds when a delegated execution
  is missing a dependency, so a queued `get` is retried across peers exactly like any
  other want, and this command does not need the daemon to be reachable to queue one.
* `--now`. Fetched synchronously, in this process, by asking every known peer's
  `GetService` RPC in turn -- `check_wanted_service` is reused directly rather than
  duplicated, so a fix to that peer loop is a fix here too. No thread safety concerns
  despite the note by its caller in `maintain.py`: that note is about the manager
  thread's own `wanted_services.pop()`, and this call never touches that set.
"""
import os

from src.commands.__by_tag import get_id
from src.commands.execute import resolve_service_hash
from src.manager.maintain import WANTED_INBOX_DIR, check_wanted_service


def _hash_to_fetch(service: str) -> str:
    resolved = get_id(service)
    return resolved if resolved else service


def get_service(service: str, now: bool = False) -> None:
    if resolve_service_hash(service):
        print(f"Service {service} is already in the local registry.")
        return

    service_hash = _hash_to_fetch(service)
    try:
        bytes.fromhex(service_hash)
    except ValueError:
        print(f"Error: '{service}' is not a known tag nor a valid service hash.")
        return

    if not now:
        os.makedirs(WANTED_INBOX_DIR, exist_ok=True)
        open(os.path.join(WANTED_INBOX_DIR, service_hash), "a").close()
        print(
            f"Queued {service_hash}; the running node will look for it among its "
            "peers (use --now to fetch it here instead)."
        )
        return

    print(f"Asking known peers for {service_hash}...")
    check_wanted_service(service_hash)

    if resolve_service_hash(service_hash):
        print(f"Service {service_hash} retrieved and stored in the local registry.")
    else:
        print(
            f"Could not get {service_hash} from any known peer (or none are reachable)."
        )
