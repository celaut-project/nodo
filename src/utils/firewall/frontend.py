"""Which high-level firewall front-end is running here, and the one command to use.

nodo writes netfilter rules directly and cannot overrule a foreign reject on the
input hook (``backends`` explains why: ``accept`` ends its own chain only). When
that happens the port has to be opened wherever the host's firewall is actually
managed, so the one genuinely useful thing nodo can add is the single command for
the front-end that is *running on this host* -- detected, never guessed.

Nothing here changes the host: it reads state and returns text. Detection is
"binary present AND reports itself active", because an installed-but-stopped
firewalld is not what is rejecting the packet, and telling the operator to
configure it would send them after the wrong thing.
"""

import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), capture_output=True, text=True, check=False, timeout=10
    )


@dataclass(frozen=True)
class Frontend:
    """A running firewall front-end and how to open a TCP port with it."""

    name: str
    command: str


def _firewalld(port: int, run: Runner) -> Optional[Frontend]:
    if not shutil.which("firewall-cmd"):
        return None
    try:
        proc = run(["firewall-cmd", "--state"])
    except Exception:
        return None
    # Exact, because the answer to --state is either "running" or "not running":
    # a substring test would read the second as the first.
    state = ((proc.stdout or "") + (proc.stderr or "")).strip().lower()
    if proc.returncode != 0 or state != "running":
        return None
    return Frontend(
        name="firewalld",
        command=(
            f"sudo firewall-cmd --permanent --add-port={port}/tcp && "
            "sudo firewall-cmd --reload"
        ),
    )


def _ufw(port: int, run: Runner) -> Optional[Frontend]:
    if not shutil.which("ufw"):
        return None
    try:
        proc = run(["ufw", "status"])
    except Exception:
        return None
    if "status: active" not in (proc.stdout or "").lower():
        return None
    return Frontend(name="ufw", command=f"sudo ufw allow {port}/tcp")


_DETECTORS = (_firewalld, _ufw)


def detect_frontend(port: int, *, run: Optional[Runner] = None) -> Optional[Frontend]:
    """The running front-end that can open ``port``, or None if there is none.

    None is the honest answer on a host whose ruleset is a hand-written nft file,
    a config-management template or a container runtime's doing: there is no
    command to name, and inventing one would be the thing that breaks the host.
    """
    runner = run or _default_runner
    for detector in _DETECTORS:
        try:
            frontend = detector(port, runner)
        except Exception:
            continue
        if frontend is not None:
            return frontend
    return None


def _firewalld_scoped(port: int, subnet: str, run: Runner) -> Optional[Frontend]:
    if not shutil.which("firewall-cmd"):
        return None
    try:
        proc = run(["firewall-cmd", "--state"])
    except Exception:
        return None
    state = ((proc.stdout or "") + (proc.stderr or "")).strip().lower()
    if proc.returncode != 0 or state != "running":
        return None
    rule = (
        f'rule family="ipv4" source address="{subnet}" '
        f'port protocol="tcp" port="{port}" accept'
    )
    return Frontend(
        name="firewalld",
        command=(
            f"sudo firewall-cmd --permanent --add-rich-rule='{rule}' && "
            "sudo firewall-cmd --reload"
        ),
    )


def _ufw_scoped(port: int, subnet: str, run: Runner) -> Optional[Frontend]:
    if not shutil.which("ufw"):
        return None
    try:
        proc = run(["ufw", "status"])
    except Exception:
        return None
    if "status: active" not in (proc.stdout or "").lower():
        return None
    return Frontend(
        name="ufw",
        command=f"sudo ufw allow from {subnet} to any port {port} proto tcp",
    )


_SCOPED_DETECTORS = (_firewalld_scoped, _ufw_scoped)

# Distinguishes "the caller has no `Frontend` to hand in" from "the caller
# already detected one, and it happened to be None" -- `open_port_advice`'s own
# `frontend` parameter needs both, and plain `None` can only mean the second.
_UNDETECTED = object()


def detect_scoped_frontend(
    port: int, subnet: str, *, run: Optional[Runner] = None
) -> Optional[Frontend]:
    """Like :func:`detect_frontend`, but the rule only ever admits ``subnet``.

    For a port that must never be reachable beyond the guests it was handed to:
    ``sudo ufw allow <port>/tcp`` is right advice for a port peers off this LAN are
    meant to reach, and wrong advice for one that is not. This asks the same
    front-ends for the source-restricted form of the same rule, so the one command
    nodo hands the operator is never wider than the port needs.
    """
    runner = run or _default_runner
    for detector in _SCOPED_DETECTORS:
        try:
            frontend = detector(port, subnet, runner)
        except Exception:
            continue
        if frontend is not None:
            return frontend
    return None


def open_port_advice(
    port: int,
    *,
    bridge: str = "",
    subnet: str = "",
    run: Optional[Runner] = None,
    frontend: Any = _UNDETECTED,
) -> List[str]:
    """The shortest useful instruction for opening ``port`` inbound on this host.

    Either one command for the detected front-end, or -- when none is running --
    a statement of the property that has to hold, short enough to paste
    somewhere that can turn it into a command for whatever manages this ruleset.

    ``frontend`` lets a caller that already ran :func:`detect_frontend` for this
    ``port`` (``_blocked_port_error`` needs the same result again, for its bare
    ``.command``) hand it straight in, rather than have this function shell out
    to the (subprocess-based) detectors a second time. Left unset, detection
    happens here as before.
    """
    if frontend is _UNDETECTED:
        frontend = detect_frontend(port, run=run)
    if frontend is not None:
        return [
            f"This host runs {frontend.name}. Open the port with:",
            f"  {frontend.command}",
        ]

    where = f" Guests reach it from {subnet} over {bridge}." if bridge and subnet else ""
    return textwrap.wrap(
        f"No running firewall front-end (firewalld, ufw) was found here, so nodo has "
        f"no command to name. What has to hold: inbound TCP {port} accepted on the "
        f"netfilter input hook, with no other base chain on that hook rejecting or "
        f"dropping it.{where} Apply that wherever this host's ruleset is managed.",
        width=78,
    )


def open_scoped_port_advice(
    port: int,
    *,
    subnet: str,
    bridge: str = "",
    run: Optional[Runner] = None,
    frontend: Any = _UNDETECTED,
) -> List[str]:
    """Like :func:`open_port_advice`, but the rule it hands over never leaves ``subnet``.

    For the plaintext gateway port: unauthenticated plain gRPC that is only ever
    supposed to answer the guests nodo itself launched. ``open_port_advice`` hands
    the operator a rule with no source restriction at all, which is correct for the
    TLS port -- it authenticates itself, and peers off this LAN are meant to reach
    it too -- and wrong here, where reachable from *anywhere* is the failure mode,
    not the fix. So the rule this advises is scoped from the start rather than
    opened wide with a promise to narrow it later.

    ``frontend`` is the same reuse escape hatch as :func:`open_port_advice`'s: a
    caller that already ran :func:`detect_scoped_frontend` for this ``port`` and
    ``subnet`` can hand the result straight in instead of paying for a second,
    identical detection.
    """
    if frontend is _UNDETECTED:
        frontend = detect_scoped_frontend(port, subnet, run=run)
    if frontend is not None:
        return [
            f"This host runs {frontend.name}. Open the port, admitting only {subnet}, with:",
            f"  {frontend.command}",
        ]

    where = f" over {bridge}" if bridge else ""
    return textwrap.wrap(
        f"No running firewall front-end (firewalld, ufw) was found here, so nodo has "
        f"no command to name. What has to hold: inbound TCP {port} accepted on the "
        f"netfilter input hook for traffic from {subnet} only{where}, with no other "
        f"base chain on that hook rejecting or dropping it, and not opened to "
        f"anything outside that subnet. Apply that wherever this host's ruleset is "
        f"managed.",
        width=78,
    )
