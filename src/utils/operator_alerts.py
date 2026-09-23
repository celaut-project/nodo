"""What this node needs its operator to *do*, in the two places an operator looks.

The problem this solves is not that nodo fails to detect these conditions. It
detects both of them well, and says so -- in ``storage/app.log``, which is a file
nobody reads until something is already broken, and at the end of a ``nodo serve``
that the operator is not watching because systemd started it. The gateway port
being closed and Java being absent are the two failures that leave a node looking
perfectly healthy while it quietly cannot do its job, and both of them were only
ever announced to a log.

So the checks move to where an operator actually is: ``nodo info``, which is the
first thing anyone types, and the TUI's OVERVIEW, which is the screen they leave
open. Same two facts, same wording, one module.

**Cheapness is a requirement, not a nicety.** ``nodo info`` runs on every
invocation and the TUI polls this on its data tick. Neither may spawn a process or
open a socket: everything here is a config read plus a handful of ``os.path``
stats, and the one ``PATH`` scan (``shutil.which``) that ``ensure_java_runtime``
already performs. Nothing probes the network, nothing runs ``java -version``, and
nothing writes.

**Derived, never cached as a verdict.** An alert is recomputed from the state it
describes every time it is asked for, so it disappears on its own when the
condition does -- a port that has since been opened and proven clears
``.gateway_notice`` through ``ConfigManager.mark_gateway_port_passed``, and an
installed JRE simply answers the next check. There is no acknowledge, dismiss or
snooze: an alert that can be silenced without fixing anything is an alert that
tells you nothing.

The TUI reads the same two conditions in Rust
(``src/commands/tui/src/alerts.rs``) rather than shelling out to this module on
every refresh. The duplication is deliberate and pinned by a test on each side
that the *criteria* match: a subprocess per tick would be a per-frame fork, and a
TUI whose banner lags the CLI by a process spawn is worse than one that agrees
with it by construction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional


#: Prefix for a line the operator has to act on. Uppercase and bracketed so it
#: survives being read at a glance in a wall of ordinary ``key: value`` output --
#: `nodo info` is a dozen lines and an alert phrased like the rest of them is an
#: alert that reads as one more fact about the node.
ACTION_REQUIRED = "[ACTION REQUIRED]"

#: Width the fix command is centered in when a firewall alert shows one in
#: place. Matches ``NOTICE_RULE`` in ``src/utils/firewall/gateway.py``, the same
#: 78-column convention every wrapped paragraph in these notices already uses.
_COMMAND_WIDTH = 78


def _command_block(command: str) -> str:
    """``command``, set apart on its own line and centered, blank lines above and below.

    The alternative -- naming the file the command lives in -- is what this
    replaces: an operator reading `nodo info` had to go open a second file to
    find one line, and that line was already sitting in memory the moment this
    alert was computed. Blank lines and centering keep it from running together
    with the sentence before and after it, the same problem ``operator_notice``
    solves for the longer, framed messages this one is a summary of.
    """
    return f"\n\n{command.center(_COMMAND_WIDTH).rstrip()}\n"


@dataclass(frozen=True)
class OperatorAlert:
    """One thing that is wrong and that only the operator can fix.

    ``key`` is stable and machine-readable so a caller can test for a specific
    alert without matching on prose. ``summary`` is one line, for ``nodo info``
    and for the TUI's banner; ``detail`` is the multi-line instructions, shown
    where there is room for them.
    """

    key: str
    summary: str
    detail: str = ""

    def as_line(self) -> str:
        """The single line `nodo info` prints."""
        return f"{ACTION_REQUIRED} {self.summary}"


def gateway_port_alert(config_manager=None, serving: Optional[bool] = None) -> Optional[OperatorAlert]:
    """The gateway port is not usable, and the node cannot serve until it is.

    The summary leads with the **consequence** rather than with the mechanism: what
    an operator has to learn from one line in a wall of output is that this node is
    not doing its job, and only then why.

    ``serving`` distinguishes the two ways that happens, because they read very
    differently from the operator's chair:

    * ``False`` -- **NOT SERVING.** No node process is answering. Nothing is
      running to be reached.
    * ``True`` -- **RUNNING BUT UNREACHABLE.** The process is up and answering
      locally, so every local check looks healthy, and no peer can get to it. This
      is the failure worth naming precisely: it is indistinguishable from a working
      node without going outside the host.
    * ``None`` -- not known here, so the wording claims only what it can:
      inaccessible from outside.

    Passed in rather than probed. ``nodo info`` already calls ``is_serving()`` on
    the line above this one and the TUI already polls the same answer, so the fact
    is free at both call sites; asking again here would put a socket connect on a
    path whose whole point is that it is two ``stat`` calls.

    Three distinguishable *causes*, each with its own fix:

    * **Unassigned.** ``network.GATEWAY_PORT`` is still ``auto``. Nothing has been
      opened and nothing can be reached; the fix is one privileged start.
    * **Assigned with a pending notice.** A port exists in config.yaml, but
      ``.gateway_notice`` is on disk, which means the last thing that looked at it
      could not open it or could not reach it. That file is written by
      ``ConfigManager._gateway_notice_unlocked`` and by ``serve.py``'s refusal, and
      removed the moment a port is *proven* reachable
      (``mark_gateway_port_passed``) or the port changes -- so its presence is
      exactly "there is an open question about this port", with no separate
      lifetime to keep in step.
    * **Assigned, settled, and nothing is listening on it.** Config is right, the
      firewall has no open question, and ``serving`` is False: the node is simply
      down. Worth its own line precisely because everything an operator would think
      to check is correct, so there is nothing to find by checking it -- and the
      node earns nothing for as long as it stays down.

    A port that is assigned with no notice beside it, on a node that *is* serving,
    is the ordinary state and produces nothing. This never probes the *network*:
    proving reachability rebuilds a network namespace
    (``src/utils/firewall/reachability.py``) and is the daemon's job, once per boot.
    Reporting a *stored verdict* is what makes this cheap enough to run on every
    `nodo info`. It opens no socket of its own either -- ``serving`` already is the
    result of one (``is_serving()`` connects to ``127.0.0.1:<port>``), and both
    callers hold that answer before they ask.
    """
    from src.utils.config import (
        GATEWAY_NOTICE_COMMAND_FILE,
        GATEWAY_NOTICE_FILE,
        ConfigManager,
        coerce_gateway_port,
    )

    manager = config_manager or ConfigManager()
    try:
        port = manager.gateway_port_or_none()
    except Exception:
        # A config that cannot be read at all is a bigger problem than this alert,
        # and one the caller's own error handling will surface. Saying nothing is
        # better than claiming a port is unassigned because YAML failed to parse.
        return None

    try:
        notice_dir = os.path.dirname(os.path.realpath(manager.config_path)) or "."
        notice_path = os.path.join(notice_dir, GATEWAY_NOTICE_FILE)
        pending = _read_text(notice_path)
        command = _read_text(os.path.join(notice_dir, GATEWAY_NOTICE_COMMAND_FILE))
    except Exception:
        pending = None
        command = None

    if coerce_gateway_port(port) is None:
        return OperatorAlert(
            key="gateway_port_unassigned",
            summary=(
                "NOT SERVING - no gateway port is assigned, so no peer can reach "
                "this node and it earns nothing. Assign and open one: sudo nodo serve"
            ),
            detail=pending or "",
        )

    if pending:
        lead = f"{_unreachable_lead(serving)} TCP {port} is not open in the host firewall, so peers cannot reach this node."
        summary = (
            f"{lead} Open it:{_command_block(command)}"
            if command
            else f"{lead} Open it: see {notice_path} for the exact command."
        )
        return OperatorAlert(
            key="gateway_port_firewall",
            summary=summary,
            detail=pending,
        )

    if serving is False:
        return OperatorAlert(
            key="gateway_port_closed",
            summary=(
                f"NOT SERVING - nothing is listening on TCP {port}, so no peer can "
                f"reach this node and it earns nothing while it is down. Start it: "
                f"sudo nodo serve"
            ),
            detail=(
                "The port is assigned and the host firewall has no open question "
                "about it, so nothing in the configuration is wrong -- there is "
                "simply no node process answering on it.\n"
                "Start it with:\n  sudo nodo serve\n"
                "If it was started and stopped on its own, storage/app.log holds "
                "why."
            ),
        )

    return None


def plaintext_gateway_port_alert(config_manager=None) -> Optional[OperatorAlert]:
    """The guest-only counterpart of ``gateway_port_alert``.

    Different audience, different alert. The TLS port is what peers and the CLI
    dial, so its notice reads "peers cannot reach this node". The plaintext port
    is never announced to either -- it exists for the services this node launches,
    handed to them in ``__config__.gateway`` -- so the thing that breaks when it is
    unreachable is every microVM this node runs, never a peer off this LAN. No
    ``serving`` parameter either: unlike the TLS port, there is no "not serving at
    all" state to distinguish from "up but unreachable" -- ``0`` just means the
    operator turned this port off, which is an ordinary configuration and not an
    alert.

    Same cheapness contract as ``gateway_port_alert``: a config read and a
    ``.gateway_plaintext_notice`` stat, nothing that touches the network.
    """
    from src.utils.config import (
        GATEWAY_PLAINTEXT_NOTICE_COMMAND_FILE,
        GATEWAY_PLAINTEXT_NOTICE_FILE,
        ConfigManager,
    )

    manager = config_manager or ConfigManager()
    try:
        port = manager.get_plaintext_gateway_port()
    except Exception:
        # Same reasoning as gateway_port_alert: a config that cannot be read is a
        # bigger problem than this alert, and not this alert's to report.
        return None

    if not port:
        # 0 (or GATEWAY_PLAINTEXT_PORT disabled) means the operator turned this
        # off; services fall back to the TLS port instead, which is its own
        # deliberate state, not a failure to report on.
        return None

    try:
        notice_dir = os.path.dirname(os.path.realpath(manager.config_path)) or "."
        notice_path = os.path.join(notice_dir, GATEWAY_PLAINTEXT_NOTICE_FILE)
        pending = _read_text(notice_path)
        command = _read_text(os.path.join(notice_dir, GATEWAY_PLAINTEXT_NOTICE_COMMAND_FILE))
    except Exception:
        pending = None
        command = None

    if not pending:
        return None

    lead = (
        f"TCP {port} (the plaintext gateway) is not reachable from the guest "
        "subnet, so services this node launches cannot call back into it."
    )
    summary = (
        f"{lead} Fix it:{_command_block(command)}"
        if command
        else f"{lead} Fix it: see {notice_path} for the exact command."
    )
    return OperatorAlert(
        key="gateway_plaintext_port_unreachable",
        summary=summary,
        detail=pending,
    )


def _unreachable_lead(serving: Optional[bool]) -> str:
    """The first words of the firewall alert: what is wrong, before why.

    Three states rather than two, because "the process is up and nobody can reach
    it" is the one an operator cannot discover from inside the host -- every local
    check answers, and the node is earning nothing.
    """
    if serving is True:
        return "RUNNING BUT UNREACHABLE -"
    if serving is False:
        return "NOT SERVING -"
    return "NOT REACHABLE FROM OUTSIDE -"


def java_alert() -> Optional[OperatorAlert]:
    """No Java runtime, so payments and reputation are silently unavailable.

    Worth its own line because of *how* it fails. Nothing crashes: the Ergo
    contract cannot settle, ``registry.contracts()`` drops it, and the node goes on
    running with no payment method at all -- which looks identical to a node that
    was deliberately configured without one. An operator who never sees this
    believes they are earning.

    The same three places ``ensure_java_runtime`` looks, in the same order, and
    deliberately reusing that function rather than restating its rules: the two
    drifting apart would mean a node that refuses to pay while ``nodo info``
    reports Java as present.
    """
    from src.utils.java_dependency import (
        JavaDependencyMissing,
        get_java_install_command,
    )

    try:
        from src.utils.java_dependency import ensure_java_runtime

        ensure_java_runtime()
        return None
    except JavaDependencyMissing:
        pass
    except Exception:
        # Any other failure is about reading the config, not about Java. Do not turn
        # it into a claim that Java is missing.
        return None

    return OperatorAlert(
        key="java_missing",
        summary=(
            "Java is not installed, so this node cannot settle payments or publish "
            "reputation. Install it with "
            f"`{get_java_install_command()}`."
        ),
        detail=(
            "Nothing fails loudly without it: the Ergo contract cannot settle, so it "
            "is dropped from what this node advertises, and the node goes on running "
            "with no payment method at all -- which is indistinguishable from a node "
            "that was configured without one.\n"
            f"Install it with:\n  {get_java_install_command()}"
        ),
    )


def java_is_available() -> bool:
    """Whether a Java runtime is reachable, as a plain bool. No subprocess.

    Separate from :func:`java_alert` for callers that want the fact rather than the
    prose -- and kept here rather than in ``java_dependency`` so the one place that
    decides "is Java here" for *display* is the one place `nodo info` and the TUI
    agree on.
    """
    return java_alert() is None


def collect(config_manager=None, serving: Optional[bool] = None) -> List[OperatorAlert]:
    """Every pending alert, in the order they should be read.

    The gateway port comes first because it is the one that stops the node
    entirely: a node that cannot serve has no use for a payment system. Its
    plaintext counterpart comes next, ahead of Java, for the same reason one rung
    down: it does not stop the node, but it does stop every service the node
    launches from being able to call back into it.

    ``serving`` is threaded through rather than asked for here, so the one caller
    that already knows it (`nodo info`, which prints it on the line above) does not
    pay for a second socket connect.
    """
    alerts = []
    for alert in (
        gateway_port_alert(config_manager, serving),
        plaintext_gateway_port_alert(config_manager),
        java_alert(),
    ):
        if alert is not None:
            alerts.append(alert)
    return alerts


def _read_text(path: str) -> Optional[str]:
    """The file's contents, or None when it is absent, empty or unreadable.

    Empty reads as absent on purpose: a zero-byte ``.gateway_notice`` (a write that
    was interrupted, a disk that filled) carries no instructions, and an alert whose
    body is a blank line tells the operator less than no alert at all.
    """
    try:
        with open(path, "r") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return text or None

