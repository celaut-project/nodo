"""Is the guest bridge in a firewalld zone? The question behind a silent failure.

An interface firewalld has no zone for is handled by the *default* zone, which on
Fedora and RHEL ends in ``reject with icmpx admin-prohibited``. That reject applies
to every packet the bridge routes -- guest to guest, and guest to the host -- and a
guest sees it as ``EHOSTUNREACH`` ("No route to host"). The parent of a launched
child then reports a dead child. ``nodo`` cannot repair it (see ``compat.py``: its
rules live in a table firewalld's ``filter_FORWARD`` does not consult), so the
operator has to, and the only way that happens is if they are told.

This module answers one narrow question, cheaply, from the daemon's start path:
*is firewalld running, and does it have a zone for this bridge?* It is a heuristic
on purpose. A bridge in some zone may still be rejected by that zone's policy, and
a host whose default zone happens to allow forwarding would be flagged without
need. ``nodo doctor`` is the authority -- it sends a real packet -- and clears the
notice this raises when that packet gets through. What this buys is that the
common failure is announced where operators look, instead of only in ``app.log``.

Nothing here changes the host. It reads firewalld's state and returns text.
"""

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

#: Name of the zone the suggested commands create. Its own zone, not ``trusted``:
#: ``trusted`` would expose every port the host listens on to every guest
#: (docs/FIREWALL.md, "Sharing the host with another firewall").
GUEST_ZONE = "nodo"


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), capture_output=True, text=True, check=False, timeout=10
    )


@dataclass(frozen=True)
class BridgeZone:
    """What firewalld says about the guest bridge.

    ``running`` is False when firewalld is absent or stopped, in which case there
    is nothing to report. ``zone`` is None when firewalld has no zone for the
    bridge, and also when the answer could not be read: ``readable`` tells those
    apart, because "I could not look" must never be raised as an alert.
    """

    running: bool
    readable: bool = True
    zone: Optional[str] = None

    @property
    def unzoned(self) -> bool:
        """firewalld is running, was asked, and has the bridge in no zone."""
        return self.running and self.readable and self.zone is None


def bridge_zone(bridge: str, *, run: Optional[Runner] = None) -> BridgeZone:
    """Ask firewalld which zone ``bridge`` is in. Never raises."""
    run = run or _default_runner
    if not shutil.which("firewall-cmd"):
        return BridgeZone(running=False)
    try:
        state = run(["firewall-cmd", "--state"])
    except Exception:
        return BridgeZone(running=False)
    # Exact: the answer is "running" or "not running", and a substring test would
    # read the second as the first.
    said = ((state.stdout or "") + (state.stderr or "")).strip().lower()
    if state.returncode != 0 or said != "running":
        return BridgeZone(running=False)

    try:
        proc = run(["firewall-cmd", f"--get-zone-of-interface={bridge}"])
    except Exception:
        return BridgeZone(running=True, readable=False)
    answer = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # "no zone" is a successful answer on some versions and an error on others.
    if "no zone" in answer.lower():
        return BridgeZone(running=True, zone=None)
    if proc.returncode != 0 or not answer:
        return BridgeZone(running=True, readable=False)
    return BridgeZone(running=True, zone=answer)


def zone_commands(bridge: str, ports: Sequence[int]) -> List[str]:
    """The commands that give ``bridge`` a zone of its own, as separate lines.

    ``--add-forward`` is what lets a guest reach another guest *through the host*:
    traffic between two interfaces of the same zone is only forwarded when the zone
    says so. It needs firewalld 1.0 or later.
    """
    zone = GUEST_ZONE
    commands = [
        f"sudo firewall-cmd --permanent --new-zone={zone}",
        f"sudo firewall-cmd --permanent --zone={zone} --add-interface={bridge}",
    ]
    for port in ports:
        commands.append(f"sudo firewall-cmd --permanent --zone={zone} --add-port={port}/tcp")
    commands.append(f"sudo firewall-cmd --permanent --zone={zone} --add-forward")
    commands.append("sudo firewall-cmd --reload")
    return commands


def one_line(commands: Sequence[str]) -> str:
    """``commands`` as a single pasteable line, for the notice's ``.cmd`` companion."""
    return " && ".join(commands)


def notice_title(bridge: str) -> str:
    return f"guests on {bridge} cannot reach each other"


def notice_body(bridge: str, commands: Sequence[str]) -> str:
    lines = [
        f"firewalld is running and has no zone for {bridge}, so it handles the guest "
        "bridge with the default zone, which rejects forwarded traffic.",
        "",
        "Every service this node launches as a dependency of another will fail, and "
        "the failure looks like a child that died on boot.",
        "",
        f"Give the bridge a zone of its own (not 'trusted': that would expose every "
        "port this host listens on to every guest):",
        "",
    ]
    lines.extend(f"  {command}" for command in commands)
    lines.extend(
        [
            "",
            "Then run 'sudo nodo doctor' to confirm. It sends a real guest-to-guest "
            "packet and clears this notice when it gets through.",
        ]
    )
    return "\n".join(lines)
