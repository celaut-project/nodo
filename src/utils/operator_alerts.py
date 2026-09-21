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


def gateway_port_alert(config_manager=None) -> Optional[OperatorAlert]:
    """The gateway port is not usable, and the node cannot serve until it is.

    Two distinguishable states, and they want different words:

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

    A port that is assigned with no notice beside it is the ordinary state and
    produces nothing. This never probes: proving reachability rebuilds a network
    namespace (``src/utils/firewall/reachability.py``) and is the daemon's job,
    once per boot. Reporting a *stored verdict* is what makes this cheap enough to
    run on every `nodo info`.
    """
    from src.utils.config import GATEWAY_NOTICE_FILE, ConfigManager, coerce_gateway_port

    manager = config_manager or ConfigManager()
    try:
        port = manager.gateway_port_or_none()
    except Exception:
        # A config that cannot be read at all is a bigger problem than this alert,
        # and one the caller's own error handling will surface. Saying nothing is
        # better than claiming a port is unassigned because YAML failed to parse.
        return None

    try:
        notice_path = os.path.join(
            os.path.dirname(os.path.realpath(manager.config_path)) or ".",
            GATEWAY_NOTICE_FILE,
        )
        pending = _read_text(notice_path)
    except Exception:
        pending = None

    if coerce_gateway_port(port) is None:
        return OperatorAlert(
            key="gateway_port_unassigned",
            summary=(
                "The gateway port is not assigned, so this node cannot serve. "
                "Run 'sudo nodo serve' once to pick and open one."
            ),
            detail=pending or "",
        )

    if pending:
        return OperatorAlert(
            key="gateway_port_firewall",
            summary=(
                f"TCP {port} must be open in the host firewall before this node can "
                f"serve. See {notice_path} for the exact command."
            ),
            detail=pending,
        )

    return None


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


def collect(config_manager=None) -> List[OperatorAlert]:
    """Every pending alert, in the order they should be read.

    The gateway port comes first because it is the one that stops the node
    entirely: a node that cannot serve has no use for a payment system.
    """
    alerts = []
    for alert in (gateway_port_alert(config_manager), java_alert()):
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

