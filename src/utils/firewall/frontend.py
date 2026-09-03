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
from typing import Callable, List, Optional, Sequence

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


def open_port_advice(
    port: int,
    *,
    bridge: str = "",
    subnet: str = "",
    run: Optional[Runner] = None,
) -> List[str]:
    """The shortest useful instruction for opening ``port`` inbound on this host.

    Either one command for the detected front-end, or -- when none is running --
    a statement of the property that has to hold, short enough to paste
    somewhere that can turn it into a command for whatever manages this ruleset.
    """
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
