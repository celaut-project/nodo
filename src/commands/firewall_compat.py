"""``nodo firewall-compat`` -- inspect, apply or remove the one hole nodo punches.

nodo filters in its own nftables table. On a host where something else owns the
forward hook -- Docker sets ``ip filter FORWARD``'s policy to DROP, ufw defaults
``DEFAULT_FORWARD_POLICY`` to DROP -- that filter is not the last word, and guests
cannot reach each other however cleanly nodo accepted them. ``NODO_FWD`` is nodo's
answer: its own chain in the compatibility table, holding one narrow accept per
guest path it needs.

This command exists because those rules live in a table nodo does not own, and
anything nodo writes there has to be something the operator can see and take back.
``status`` is what is in there, ``apply`` puts it in, ``remove`` takes it out.

See ``src/utils/firewall/compat.py`` for what it reaches -- and for firewalld,
which it does not.
"""

import os
import sys

USAGE = "Usage: nodo firewall-compat [status|apply|remove]"


def _settings():
    from src.utils.config import ConfigManager

    env_manager = ConfigManager()
    return (
        str(env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch")),
        str(env_manager.get("virtualizers.ch.FORWARD_COMPAT", "auto")),
    )


def _print_state(state) -> None:
    for line in state.describe():
        print(f"  {line}", flush=True)


def firewall_compat_command(subcommand=None) -> None:
    action = (subcommand or "status").strip().lower()
    if action not in ("status", "apply", "remove"):
        print(USAGE, flush=True)
        return

    try:
        bridge, configured = _settings()
    except Exception as e:
        print(f"Could not read the node config: {e}", flush=True)
        return

    from src.utils.firewall.compat import (
        CompatMode,
        compat_state,
        ensure_compat,
        remove_compat,
    )

    try:
        mode = CompatMode.parse(configured)
    except ValueError as e:
        print(f"{e}", flush=True)
        return

    print(f"\nForward compatibility for {bridge} (FORWARD_COMPAT={mode.value}):", flush=True)

    if action == "status":
        _print_state(compat_state(bridge, mode))
        # Reading iptables needs root too, so say which answer this is rather than
        # printing an empty ruleset as if it were the host's.
        if os.geteuid() != 0:
            print(
                "  Read without root, so this only reflects what this user can see; "
                "run it with sudo for the host's real answer.",
                flush=True
            )
        return

    if os.geteuid() != 0:
        print("  This changes the host firewall and needs root. Re-run with sudo.", flush=True)
        sys.exit(1)

    if action == "apply":
        # Explicit request, so it is applied whatever auto would have decided: the
        # operator asking for it is a better signal than a ruleset scan.
        state = ensure_compat(bridge, CompatMode.ON, log=lambda message: None)
    else:
        state = remove_compat(bridge, log=lambda message: None)

    _print_state(state)
    if state.error:
        sys.exit(1)
