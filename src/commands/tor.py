"""``nodo tor`` -- install nodo's own Tor and show whether the node is using it.

  nodo tor install   install Tor under dependencies.tor.RUNTIME_ROOT (nothing system-wide)
  nodo tor status    the settings, the installed copy, the onion address and what the running node says

Turning Tor on is a setting, not a command: ``nodo config set routing.tor.ENABLED=true``
(or the CONFIG page of ``nodo tui``). ``install`` is for installing it ahead of time.
"""

import json
import os
import subprocess
import sys
from typing import List, Optional

from src.routing.policy import announce_onion, hidden_mode
from src.routing.tor import _tor_version, status_file, tor_binary, tor_mode
from src.routing.tor_install import (
    TorInstallError,
    ensure_tor_installed,
    install_command,
    managed_binary,
    tor_installed,
)

USAGE = """Usage:
  nodo tor install
  nodo tor status [--json]"""


def _daemon_state() -> dict:
    """What the running node last wrote about its Tor, or ``{}`` if it is not running."""
    try:
        with open(status_file()) as f:
            data = json.load(f)
        os.kill(int(data["pid"]), 0)  # raises if the node that wrote it is gone
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def status() -> dict:
    binary = tor_binary()
    installed = tor_installed(binary)
    env = os.environ.copy()
    if binary == managed_binary():
        env["LD_LIBRARY_PATH"] = str(binary.parent)
    version = _tor_version(binary, env) if installed else None
    daemon = _daemon_state()
    return {
        "enabled": tor_mode(),
        "binary": str(binary),
        "installed": installed,
        "version": ".".join(map(str, version)) if version else None,
        "node": daemon.get("state", "not running"),
        "detail": daemon.get("detail", ""),
        "hidden": hidden_mode(),
        "announce": announce_onion(),
        "onion": daemon.get("onion"),
    }


def tor(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    sub = argv.pop(0) if argv else "status"

    if sub == "install":
        try:
            ensure_tor_installed()
        except TorInstallError as e:
            print(f"Error: {e}")
            return 1
        print("Tor is installed. A running node starts using it after `nodo daemon restart`.")
        return 0

    if sub == "status":
        info = status()
        if "--json" in argv:
            print(json.dumps(info))
            return 0
        print(f"routing.tor.ENABLED  {info['enabled']}")
        print(f"binary               {info['binary']}")
        print(f"installed            {'yes, Tor ' + info['version'] if info['installed'] else 'no'}")
        print(f"node                 {info['node']}" + (f" ({info['detail']})" if info['detail'] else ""))
        print(f"routing.HIDDEN       {info['hidden']}")
        print(f"announce onion       {info['announce']}")
        if info["onion"]:
            print(f"onion address        {info['onion']}")
        if not info["installed"]:
            print(f"\nInstall it with: nodo tor install   ({install_command()})")
        return 0

    print(USAGE)
    return 1
