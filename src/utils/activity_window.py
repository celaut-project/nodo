"""The hours of the day this node accepts work in.

A node on a machine somebody also uses has hours: rent the PC out overnight, keep it to
yourself while you are working. Nothing else in the config expresses that. Scarcity
pricing makes a busy machine expensive and `low_demand` gates only the opportunistic
fallback; neither of them ever refuses a paid workload, and neither of them knows what
time it is.

What "closed" means is deliberately narrow. Closed refuses *new* work: a client's
StartService, a peer's cost request, a peer's capacity probe, and a running instance
asking for a child. It says nothing about the instances already running -- unless
`ON_CLOSE` is `stop`, which the manager tick reads to reap them (see
`src/manager/maintain.py`).

Work descended from a dev client is exempt, which is what keeps `nodo execute`, the core
services and `nodo pack` working at four in the morning: the window is about renting this
machine out, not about locking its owner out of it. The exemption is the caller's to
apply -- this module only answers what time it is -- and `descends_from_dev_client` in
`src/manager/manager.py` is what answers it.

Times are the host's local time, read at every call rather than cached: the answer
depends on the clock, not only on the config. The window itself is the one the node
booted with, and an edit reaches it through the restart `nodo tui` performs.

The schedule is a *list* of windows (`activity_window.WINDOWS`), not one: a node rented
out overnight and again over a weekday lunch break has two separate open stretches that
share no arithmetic, and forcing them into a single START/END pair meant the second
stretch could not be expressed at all. The node is open whenever the clock falls inside
*any* configured window; `ON_CLOSE` is one policy shared by all of them, because what
happens to running work when the node closes does not depend on which window it was in.
"""
from datetime import datetime, time as clock
from typing import List, Optional, Tuple

from src.utils.config import ConfigManager

env_manager = ConfigManager()


SECTION = "activity_window"

# What closing time does to work already running.
ON_CLOSE_REFUSE = "refuse"
ON_CLOSE_STOP = "stop"
DEFAULT_ON_CLOSE = ON_CLOSE_REFUSE

# Said once per process rather than per call: this is read on every admission decision,
# and a config typo would otherwise print a line per launch.
_malformed_announced = False


def _log(message: str) -> None:
    """Log, importing the logger only when there is something to say.

    Not a module-level import, and the cycle it would close is not obvious: this module
    is read by `validate_host_policy_config`, which runs inside
    `ConfigManager.load_config`, and `src.utils.logger` asks the config for `STORAGE`
    while it is still being imported. Importing the logger from here at module scope
    therefore reaches a half-initialised `src.utils.logger` on the very first config
    load of the process.
    """
    from src.utils.logger import LOGGER

    LOGGER(message)


def _text(key: str, default: str) -> str:
    try:
        value = env_manager.get(f"{SECTION}.{key}", default)
    except Exception:
        return default
    if value is None:
        return default
    return str(value).strip()


def parse_clock(text: str) -> Optional[clock]:
    """``HH:MM`` as a time of day, or None when it is not one.

    Accepts what an operator plausibly types -- ``7:00``, ``07:00``, ``07:00:30`` -- and
    nothing else. ``24:00`` is not a time; midnight is ``00:00``, and a window ending
    at midnight is expressed by wrapping (see :func:`is_open`).
    """
    if not text:
        return None
    parts = text.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    try:
        return clock(*numbers)
    except ValueError:
        return None


def is_enabled() -> bool:
    try:
        return bool(env_manager.get(f"{SECTION}.ENABLED", False))
    except Exception:
        return False


def windows() -> List[Tuple[clock, clock]]:
    """The configured windows, or ``[]`` when there is none to be outside of.

    An empty list covers every way of saying "always open": the section switched off,
    no entries at all, an entry whose start equals its end, and -- defensively -- an
    entry that does not parse. A malformed schedule opens the node rather than closing
    it: the config validator rejects one at load (`validate_host_policy_config`), so
    reaching here at all means something wrote the file behind the node's back, and
    taking the node off the network over it would be a silent outage where a log line
    is enough. One bad entry voids the whole schedule rather than just itself, so a
    typo cannot silently narrow the hours to whatever else happened to parse.
    """
    global _malformed_announced

    if not is_enabled():
        return []

    raw = env_manager.get(f"{SECTION}.WINDOWS", []) or []
    if not isinstance(raw, list) or not raw:
        return []

    parsed: List[Tuple[clock, clock]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            entry = {}
        start_text = str(entry.get("START") or "").strip()
        end_text = str(entry.get("END") or "").strip()
        start = parse_clock(start_text)
        end = parse_clock(end_text)
        if start is None or end is None:
            if not _malformed_announced:
                _malformed_announced = True
                _log(
                    f"[WINDOW] {SECTION}.WINDOWS has an entry START={start_text!r} / "
                    f"END={end_text!r} that is not a pair of HH:MM times; this node is "
                    "treating its whole schedule as always open. Fix the window or "
                    "switch the section off."
                )
            return []
        if start != end:
            parsed.append((start, end))

    _malformed_announced = False
    return parsed


def is_open(now: Optional[datetime] = None) -> bool:
    """Whether this node takes new work at ``now`` (default: local time now).

    Open whenever ``now`` falls inside *any* configured window. Within one window,
    START is inclusive and END exclusive, and a window whose end is before its start
    wraps around midnight: 22:00 to 06:00 is one night, not an empty set. Wrapping is
    the whole reason a window is not a plain ``start <= t < end`` -- overnight is the
    case an operator renting out a personal PC actually wants.
    """
    bounds = windows()
    if not bounds:
        return True
    moment = (now or datetime.now()).time()
    for start, end in bounds:
        if start < end:
            if start <= moment < end:
                return True
        elif moment >= start or moment < end:
            return True
    return False


def stops_running_instances() -> bool:
    """Whether closing time reaps what is already running (``ON_CLOSE: stop``).

    Anything other than `stop` -- including a value nobody recognises -- leaves running
    work alone. Destroying an instance is the irreversible half of this feature, so it
    happens only when the config asks for it by name.
    """
    return _text("ON_CLOSE", DEFAULT_ON_CLOSE).lower() == ON_CLOSE_STOP


def closed_reason() -> str:
    """Why a refusal happened, in the terms the operator configured it in.

    Carried back to whoever asked -- a client, or a peer's balancer -- so a refused
    launch reads as a closed node rather than as a broken one.
    """
    bounds = windows()
    if not bounds:
        return "This node is not accepting new work."
    spans = " or ".join(
        f"{start.strftime('%H:%M')} and {end.strftime('%H:%M')}" for start, end in bounds
    )
    return (
        f"This node is outside its activity window: it accepts new work between "
        f"{spans} local time ({SECTION} in config.yaml)."
    )
