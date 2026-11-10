"""Give the guest bridge what firewalld needs to forward it, at install time.

Run by ``install.sh``. On a host where firewalld is running, nodo's accepts are
not enough: ``inet firewalld filter_FORWARD`` rejects forwarded guest traffic
afterwards, so a service's dependency looks dead and network tag ``*`` gets no
Internet (#534, #624, #639). The fix is firewalld's own configuration -- a zone for
the bridge that forwards, and a policy from it to every other zone -- and the
installer is the moment that has root and an operator watching.

``apply`` writes it (idempotent; root). ``print`` writes the commands (without sudo) to
stdout as one line, for the unprivileged install's list of steps that need root. Both do
nothing on a host without a running firewalld, or one already set up. Set
``NODO_SKIP_FIREWALLD=1`` to have the installer leave firewalld alone.

Invoked directly rather than through the ``nodo`` wrapper, like
``assign_gateway_port.py``, and never fatal.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ports(env):
    ports = []
    for read in (env.gateway_port_or_none, env.get_plaintext_gateway_port):
        try:
            port = read()
        except Exception:
            continue
        if isinstance(port, int) and port > 0 and port not in ports:
            ports.append(port)
    return ports


def main(argv) -> int:
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    from src.utils.config import ConfigManager
    from src.utils.firewall import firewalld_bridge as fwd

    mode = argv[0] if argv else "print"
    root = argv[1] if len(argv) > 1 else _ROOT
    if os.environ.get("NODO_SKIP_FIREWALLD") == "1":
        return 0

    env = ConfigManager(os.path.join(root, "config.yaml"))
    bridge = str(env.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch"))
    verdict = fwd.assess(bridge)
    if not verdict.running or (verdict.conclusive and not verdict.blocked):
        return 0
    ports = _ports(env)

    if mode == "print":
        # Without sudo: install.sh runs the line in one 'sudo sh -c', so every root
        # step it lists starts with sudo. No single quote can appear in it (a zone,
        # a policy, an interface name and port numbers).
        print(fwd.one_line(fwd.setup_commands(bridge, ports, sudo=False)))
        return 0

    print(
        f"firewalld is running: giving {bridge} a zone of its own ('{fwd.GUEST_ZONE}', "
        f"with forwarding) and a policy out of it ('{fwd.EGRESS_POLICY}'), so guests can "
        "reach each other and the Internet. nodo's per-VM rules still decide what each "
        "guest may reach. NODO_SKIP_FIREWALLD=1 skips this.",
        flush=True,
    )
    try:
        done = fwd.apply(bridge, ports)
    except Exception as e:
        print(f"firewalld was not changed: {e}", file=sys.stderr, flush=True)
        print("Run these yourself:", file=sys.stderr, flush=True)
        for command in fwd.setup_commands(bridge, ports):
            print(f"  {command}", file=sys.stderr, flush=True)
        return 0
    for step in done:
        print(f"  firewalld: {step}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
