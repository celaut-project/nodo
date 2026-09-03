"""Assign nodo's gateway port, once, as an explicit privileged step.

Run by ``install.sh`` while it still has root and the operator is still watching
the terminal. Picking the port writes a rule into the host's firewall and a value
into ``config.yaml``; that used to happen as a side effect of loading the config,
so any privileged ``nodo`` invocation -- including the completion helper the shell
runs on a Tab keypress -- could do it. The two places where it is the *intent* ask
for it instead: here and the daemon's start path.

Invoked directly rather than through the ``nodo`` wrapper, like
``completion.py``, so it does not pull in the gateway / grpc / virtualizer graph
or trip the KYA prompt during an install.

Never fatal. A failure here costs an unassigned port, which the daemon reports
with instructions; taking down the whole install for it would be worse.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv) -> int:
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    from src.utils.config import GATEWAY_NOTICE_FILE, ConfigManager
    from src.utils.firewall.gateway import (
        drain_operator_notices,
        flush_operator_notices,
    )

    # The install directory, not the caller's cwd: the installer runs this from
    # wherever it happens to be standing.
    root = argv[0] if argv else _ROOT
    env = ConfigManager(os.path.join(root, "config.yaml"))
    port = None
    try:
        port = env.assign_gateway_port_if_unset()
    except Exception as e:
        print(f"nodo: could not assign a gateway port: {e}", file=sys.stderr, flush=True)

    if os.path.exists(os.path.join(root, GATEWAY_NOTICE_FILE)):
        # install.sh prints that file as its very last act, so printing the alert
        # here as well would put it in the middle of the install output too -- which
        # is the thing being fixed. Dropped, not flushed.
        drain_operator_notices()
    else:
        # No file to fall back on, so the alert has to go out here or nowhere.
        flush_operator_notices()

    if port:
        print(f"nodo: gateway port {port} is assigned.", flush=True)
    return 0


def _drop_own_directory_from_path() -> None:
    """Keep ``src/commands`` off sys.path -- see completion.py for why it matters."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != here]


if __name__ == "__main__":
    _drop_own_directory_from_path()
    raise SystemExit(main(sys.argv[1:]))
