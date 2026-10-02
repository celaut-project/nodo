"""``nodo config`` -- read and change config.yaml without a terminal UI.

Until this command, the only supported way to *change* a serving node's
configuration was ``nodo tui`` (the CONFIG, PRICING, CELL, ENERGY and SCHEDULE
pages), and the only way to read it was ``nodo envs``, which prints the whole
file. Neither is usable by a script or an AI agent: one needs a PTY and a human
at the keys, the other has no addressing and no structured output.

Every write here is the **same transaction** the TUI applies
(``apply_config_change`` in src/commands/tui/src/app.rs, documented in its
README under "Applying a change"):

1. ``config.yaml`` is snapshotted to ``config-<UTC stamp>-<nnnn>.yaml`` beside it
   (``backup_config_file``, ten kept -- the same file the TUI writes);
2. the change is written with nodo's configured ``yq``, in place, comments kept,
   **one** invocation however many keys it spans, values passed through the
   environment and read with ``env()`` so they keep their YAML type and can never
   be read as yq syntax;
3. if a node is serving, ``nodo daemon restart`` runs and the gateway port is
   waited on until it answers again;
4. if the node does not come back, the snapshot is put back and the node is
   restarted on it.

So, as in the TUI, a serving node can only be reconfigured as root, and a node
that is not serving is simply edited. The exit status says which happened:
0 applied, 1 refused or reverted.

Paths are dotted, with ``[n]`` for list elements: ``network.GATEWAY_PORT``,
``core_services[1].id``. Paths that look like secrets (``mnemonic``,
``password``, ``secret``, ``private_key``, ``api_key``, ``token``) are masked on
read unless ``--show-secrets`` is given, exactly as the TUI masks them.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

from src.commands import _catalogue as catalogue

Segment = Union[str, int]

RESTART_READY_TIMEOUT = 120  # NODE_READY_TIMEOUT in the TUI
MASK = "********"

_SEGMENT = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

USAGE = """Usage:
  nodo config get [<path>] [--json] [--show-secrets]
  nodo config set <path>=<value> [<path>=<value> ...] [--json]
  nodo config append <list path> <value> [--json]
  nodo config remove <path>[<index>] [--json]
  nodo config profile [<profile>] [--apply] [--json]

<value> is YAML: true, 5000, 1.5, null, [], "quoted text".
Every write is backed up, and a serving node is restarted onto it (needs root)
and rolled back if it does not come back."""


# --- the profile catalogue ----------------------------------------------------
#
# The CELL page's postures, ordered from the most closed to the most open. A copy
# of PROFILES in src/commands/tui/src/cell.rs, which is the source: the test suite
# parses that file and fails if the two disagree (tests/test_commands_config_edit.py).
# A profile writes policy only -- never an identity, a wallet, a path or a port.

PROFILES: List[Dict[str, Any]] = [
    {"id": "just-me", "label": "JUST ME",
     "blurb": "I run my own things here. Nothing from outside, nothing spent outside.",
     "writes": [
         ("client.ACCEPT_NEW_DEPOSITS", "false"),
         ("network.DISABLE_EXPOSE_OUTSIDE", "true"),
         ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
         ("network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE", "false"),
         ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
         ("network.DELEGATE_EXECUTION", "false"),
         ("deposits.AUTOMATIC_REFILL", "false"),
         ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
         ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false"),
         ("service_networks.blacklist", "[]"),
         ("service_networks.whitelist", "[]"),
         ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
         ("pricing.SCARCITY_CURVE", "1.0"),
         ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
         ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "0"),
         ("costs.ALLOW_DEBT", "false"),
         ("misc.VALIDATE_ON_IMPORT", "true"),
         ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
         ("virtualizers.qemu.ENABLE", "false"),
         ("workload_admission.POLICY", "fail_fast"),
         ("workload_admission.ON_UNSATISFIABLE", "reject"),
         ("low_demand.ENABLED", "false"),
         ("host_limits.ENABLED", "true"),
         ("general_flags.SIMULATE_PAYMENTS", "false"),
         ("logs.DEBUG_MODE", "false"),
         ("logs.MEMORY_LOGS", "false"),
         ("logs.TUNNEL_LOGS", "false"),
     ]},
    {"id": "cautious", "label": "CAUTIOUS RENTER",
     "blurb": "I will rent this machine out, but on a short leash.",
     "writes": [
         ("client.ACCEPT_NEW_DEPOSITS", "true"),
         ("network.DISABLE_EXPOSE_OUTSIDE", "true"),
         ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
         ("network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE", "false"),
         ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
         ("network.DELEGATE_EXECUTION", "false"),
         ("deposits.AUTOMATIC_REFILL", "false"),
         ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "true"),
         ("network.VERIFY_GATEWAY_REACHABILITY", "true"),
         ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false"),
         ("service_networks.blacklist", "[]"),
         ("service_networks.whitelist", "[]"),
         ("pricing.SCARCITY_MAX_MULTIPLIER", "10"),
         ("pricing.SCARCITY_CURVE", "2.0"),
         ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
         ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "4500000"),
         ("costs.ALLOW_DEBT", "false"),
         ("misc.VALIDATE_ON_IMPORT", "true"),
         ("hashing.CHECK_INTEGRITY_ON_SERVE", "true"),
         ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "deny"),
         ("builder.TRUST_METADATA_ARCHITECTURE", "false"),
         ("virtualizers.qemu.ENABLE", "true"),
         ("workload_admission.POLICY", "fail_fast"),
         ("workload_admission.ON_UNSATISFIABLE", "reject"),
         ("low_demand.ENABLED", "false"),
         ("host_limits.ENABLED", "true"),
         ("general_flags.SIMULATE_PAYMENTS", "false"),
         ("logs.DEBUG_MODE", "false"),
         ("logs.MEMORY_LOGS", "false"),
         ("logs.TUNNEL_LOGS", "false"),
     ]},
    {"id": "open-renter", "label": "OPEN RENTER",
     "blurb": "I want this machine earning: reachable, delegating, priced by load.",
     "writes": [
         ("client.ACCEPT_NEW_DEPOSITS", "true"),
         ("network.DISABLE_EXPOSE_OUTSIDE", "false"),
         ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
         ("network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE", "false"),
         ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
         ("network.DELEGATE_EXECUTION", "true"),
         ("deposits.AUTOMATIC_REFILL", "true"),
         ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "true"),
         ("network.VERIFY_GATEWAY_REACHABILITY", "true"),
         ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "true"),
         ("service_networks.blacklist", "[]"),
         ("service_networks.whitelist", "[]"),
         ("pricing.SCARCITY_MAX_MULTIPLIER", "10"),
         ("pricing.SCARCITY_CURVE", "1.0"),
         ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
         ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "4500000"),
         ("costs.ALLOW_DEBT", "false"),
         ("misc.VALIDATE_ON_IMPORT", "true"),
         ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
         ("virtualizers.ch.SECURITY.DEVICE_NODES_POLICY", "deny"),
         ("builder.TRUST_METADATA_ARCHITECTURE", "false"),
         ("virtualizers.qemu.ENABLE", "true"),
         ("workload_admission.POLICY", "fail_fast"),
         ("workload_admission.ON_UNSATISFIABLE", "reject"),
         ("low_demand.ENABLED", "true"),
         ("host_limits.ENABLED", "false"),
         ("general_flags.SIMULATE_PAYMENTS", "false"),
         ("logs.DEBUG_MODE", "false"),
         ("logs.MEMORY_LOGS", "false"),
         ("logs.TUNNEL_LOGS", "false"),
     ]},
    {"id": "lan-lab", "label": "LAN LAB",
     "blurb": "A few machines on my own network, sharing capacity for free.",
     "writes": [
         ("client.ACCEPT_NEW_DEPOSITS", "true"),
         ("network.DISABLE_EXPOSE_OUTSIDE", "false"),
         ("network.ISOLATE_INTERNAL_CHILDREN", "false"),
         ("network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE", "false"),
         ("network.ANNOUNCE_PRIVATE_ADDRESSES", "true"),
         ("network.DELEGATE_EXECUTION", "true"),
         ("deposits.AUTOMATIC_REFILL", "false"),
         ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
         ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "true"),
         ("service_networks.blacklist", "[]"),
         ("service_networks.whitelist", "[]"),
         ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
         ("pricing.SCARCITY_CURVE", "1.0"),
         ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.8"),
         ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "1000000"),
         ("costs.ALLOW_DEBT", "false"),
         ("misc.VALIDATE_ON_IMPORT", "true"),
         ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
         ("virtualizers.qemu.ENABLE", "true"),
         ("workload_admission.POLICY", "full"),
         ("workload_admission.ON_UNSATISFIABLE", "warn"),
         ("low_demand.ENABLED", "true"),
         ("host_limits.ENABLED", "false"),
         ("general_flags.SIMULATE_PAYMENTS", "false"),
         ("logs.DEBUG_MODE", "false"),
         ("logs.MEMORY_LOGS", "false"),
         ("logs.TUNNEL_LOGS", "false"),
     ]},
    {"id": "workbench", "label": "WORKBENCH",
     "blurb": "I am developing against this node. Nothing here is real money.",
     "writes": [
         ("client.ACCEPT_NEW_DEPOSITS", "false"),
         ("network.DISABLE_EXPOSE_OUTSIDE", "true"),
         ("network.ISOLATE_INTERNAL_CHILDREN", "true"),
         ("network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE", "false"),
         ("network.ANNOUNCE_PRIVATE_ADDRESSES", "false"),
         ("network.CONSIDER_DEV_AS_INTERNAL", "true"),
         ("network.DELEGATE_EXECUTION", "false"),
         ("deposits.AUTOMATIC_REFILL", "false"),
         ("general_flags.SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", "false"),
         ("communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS", "false"),
         ("service_networks.blacklist", "[]"),
         ("service_networks.whitelist", "[]"),
         ("pricing.SCARCITY_MAX_MULTIPLIER", "1"),
         ("pricing.SCARCITY_CURVE", "1.0"),
         ("free_tier.FREE_WHILE_SCARCITY_BELOW", "0.0"),
         ("free_tier.CREDIT_MU_PER_NEW_CLIENT", "0"),
         ("costs.ALLOW_DEBT", "true"),
         ("misc.VALIDATE_ON_IMPORT", "true"),
         ("hashing.CHECK_INTEGRITY_ON_SERVE", "false"),
         ("virtualizers.qemu.ENABLE", "true"),
         ("workload_admission.POLICY", "full"),
         ("workload_admission.ON_UNSATISFIABLE", "warn"),
         ("low_demand.ENABLED", "false"),
         ("host_limits.ENABLED", "false"),
         ("general_flags.SIMULATE_PAYMENTS", "true"),
         ("logs.DEBUG_MODE", "true"),
         ("logs.MEMORY_LOGS", "true"),
         ("logs.TUNNEL_LOGS", "true"),
     ]},
]


# --- paths and values -------------------------------------------------------

def parse_path(path: str) -> List[Segment]:
    """``a.b[1].c`` -> ``["a", "b", 1, "c"]``. Raises ValueError on a malformed path."""
    text = path.strip()
    if not text:
        raise ValueError("Empty config path.")
    segments: List[Segment] = []
    position = 0
    while position < len(text):
        if text[position] == ".":
            if position == 0 or text[position - 1] == "." or position == len(text) - 1:
                raise ValueError(f"Malformed config path: {path!r}")
            position += 1
            continue
        match = _SEGMENT.match(text, position)
        if not match:
            raise ValueError(f"Malformed config path: {path!r}")
        segments.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        position = match.end()
    return segments


def format_path(segments: List[Segment]) -> str:
    out = ""
    for segment in segments:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            out += ("." if out else "") + segment
    return out


def yq_path_expression(segments: List[Segment]) -> str:
    """The TUI's ``yq_path_expression``: keys JSON-quoted, so none is yq syntax."""
    return "." + "".join(
        f"[{segment}]" if isinstance(segment, int) else f"[{json.dumps(segment)}]"
        for segment in segments
    )


def is_secret_path(path: str) -> bool:
    """The TUI's ``is_secret_path``, so the two mask the same keys."""
    normalized = path.lower()
    if any(marker in normalized for marker in ("mnemonic", "password", "secret", "private_key", "api_key")):
        return True
    parts = [p for p in re.split(r"[.\]]", normalized) if p and not p.lstrip("[").isdigit()]
    leaf = parts[-1].lstrip("[") if parts else normalized
    return leaf == "token" or leaf.endswith("_token")


def parse_value(text: str) -> Any:
    """A value as the TUI's editor reads it: YAML, so types survive."""
    try:
        return yaml.safe_load(text) if text.strip() else ""
    except yaml.YAMLError as e:
        raise ValueError(f"Not a YAML value: {text!r} ({e})")


def value_at(document: Any, segments: List[Segment]) -> Tuple[bool, Any]:
    """(found, value) at ``segments`` in ``document``."""
    current = document
    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return False, None
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]
    return True, current


def _same(a: Any, b: Any) -> bool:
    """Equality as serde_yaml's ``Value`` has it: ``1``, ``1.0`` and ``true`` differ."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def flatten(document: Any, prefix: List[Segment] = None) -> List[Tuple[str, Any]]:
    """Every scalar or empty collection, as the CONFIG page lists them."""
    prefix = prefix or []
    if isinstance(document, dict) and document:
        out = []
        for key, value in document.items():
            out.extend(flatten(value, prefix + [str(key)]))
        return out
    if isinstance(document, list) and document:
        out = []
        for index, value in enumerate(document):
            out.extend(flatten(value, prefix + [index]))
        return out
    return [(format_path(prefix), document)]


def mask(document: Any, prefix: str = "") -> Any:
    if isinstance(document, dict):
        return {k: mask(v, f"{prefix}.{k}" if prefix else str(k)) for k, v in document.items()}
    if isinstance(document, list):
        return [mask(v, f"{prefix}[{i}]") for i, v in enumerate(document)]
    if document not in (None, "") and is_secret_path(prefix):
        return MASK
    return document


# --- reading the file -------------------------------------------------------

def config_path() -> str:
    from src.utils.config import ConfigManager
    return os.path.realpath(ConfigManager().config_path)


def read_document(path: Optional[str] = None) -> Any:
    """config.yaml as it is on disk -- not the interpolated view the node loaded."""
    with open(path or config_path()) as f:
        return yaml.safe_load(f) or {}


def _yq_binary() -> Optional[str]:
    from src.utils.config import ConfigManager
    configured = ConfigManager().get("dependencies.yq.BIN")
    if configured and os.path.isfile(str(configured)) and os.access(str(configured), os.X_OK):
        return str(configured)
    return shutil.which("yq")


# --- the transaction --------------------------------------------------------

def _gateway_port(document: Any) -> Optional[int]:
    found, value = value_at(document, ["network", "GATEWAY_PORT"])
    try:
        port = int(value) if found else None
    except (TypeError, ValueError):
        return None
    return port if port and port > 0 else None


def _serving_on(port: Optional[int]) -> bool:
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _systemd_state() -> str:
    try:
        result = subprocess.run(
            ["systemctl", "show", "nodo.service", "--property=ActiveState", "--value"],
            capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return "absent"  # no systemd: nothing to restart but what the port says
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def restart_required(serving: bool, state: str) -> bool:
    """The TUI's ``restart_decision``; raises when the state cannot be determined."""
    if state in ("active", "activating", "reloading", "deactivating"):
        return True
    if state in ("inactive", "failed", "absent") and not serving:
        return False
    if serving:
        return True
    raise RuntimeError("Cannot determine nodo.service state; configuration unchanged. "
                       "Check `nodo daemon status`.")


def _restart() -> Tuple[bool, str]:
    from src.commands.daemon import daemon_command
    captured = StringIO()
    with redirect_stdout(captured):
        ok = daemon_command("restart", None)
    return ok, captured.getvalue().strip()


def _wait_until_serving(path: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _serving_on(_gateway_port(read_document(path))):
                return True
        except (OSError, yaml.YAMLError):
            pass
        time.sleep(1)
    return False


def _forget_gateway_verdicts(before: Any, after: Any) -> None:
    """A moved port has never been proven reachable (ConfigManager.set does this)."""
    from src.utils.config import ConfigManager
    moved_tls = value_at(before, ["network", "GATEWAY_PORT"]) != value_at(after, ["network", "GATEWAY_PORT"])
    moved_plain = value_at(before, ["network", "GATEWAY_PLAINTEXT_PORT"]) != \
        value_at(after, ["network", "GATEWAY_PLAINTEXT_PORT"])
    try:
        if moved_tls:
            ConfigManager().clear_gateway_port_passed()
        elif moved_plain:
            ConfigManager().clear_plaintext_gateway_port_passed()
    except Exception:
        pass


def apply_change(expression: str, values: List[str], label: str,
                 path: Optional[str] = None) -> Dict[str, Any]:
    """Back up, write with yq, restart if serving, roll back if it does not come up.

    Returns ``{"ok", "label", "outcome", "backup", "error"}`` where ``outcome`` is
    ``"not-running"`` (written, nothing to restart), ``"restarted"``, or
    ``"refused"`` / ``"reverted"`` when ``ok`` is False.
    """
    from src.utils.config import backup_config_file

    path = path or config_path()
    result: Dict[str, Any] = {"ok": False, "label": label, "outcome": "refused",
                              "backup": None, "error": None}
    yq = _yq_binary()
    if not yq:
        result["error"] = "yq not found (dependencies.yq.BIN, or yq on PATH)."
        return result
    try:
        before = read_document(path)
    except (OSError, yaml.YAMLError) as e:
        result["error"] = f"Could not read {path}: {e}"
        return result
    try:
        was_serving = restart_required(_serving_on(_gateway_port(before)), _systemd_state())
    except RuntimeError as e:
        result["error"] = str(e)
        return result
    if was_serving and os.geteuid() != 0:
        result["error"] = ("Configuration unchanged: nodo is serving and restarting it onto the "
                           "change requires root. Run with sudo.")
        return result

    try:
        backup = backup_config_file(path)
    except OSError as e:
        result["error"] = f"Could not create config backup: {e}"
        return result
    result["backup"] = backup

    env = dict(os.environ)
    for index, value in enumerate(values):
        env[f"NODO_CLI_V{index}"] = value
    completed = subprocess.run([yq, "e", "-i", expression, path],
                               env=env, capture_output=True, text=True)
    if completed.returncode != 0:
        result["error"] = f"yq could not update configuration: {completed.stderr.strip()}"
        if backup:
            os.remove(backup)  # nothing changed; the snapshot records no change
        return result

    try:
        _forget_gateway_verdicts(before, read_document(path))
    except (OSError, yaml.YAMLError):
        pass

    if not was_serving:
        result.update(ok=True, outcome="not-running")
        return result

    ok, output = _restart()
    if ok and _wait_until_serving(path, RESTART_READY_TIMEOUT):
        result.update(ok=True, outcome="restarted")
        return result

    if not ok:
        reason = output or "nodo daemon restart failed"
    else:
        reason = f"nodo did not come back within {RESTART_READY_TIMEOUT}s"
    result["outcome"] = "reverted"
    if backup:
        try:
            shutil.copy2(backup, path)
            os.remove(backup)
            result["backup"] = None
            result["error"] = f"{label} NOT applied: {reason} -- config.yaml restored."
        except OSError as e:
            result["error"] = (f"{label} NOT applied: {reason} -- COULD NOT RESTORE config.yaml "
                               f"({e}); the previous file is {backup}")
    else:
        result["error"] = f"{label} NOT applied: {reason}"
    if ok:
        _restart()  # best effort: back onto the configuration that was working
    return result


def _report(result: Dict[str, Any], as_json: bool, extra: Optional[Dict[str, Any]] = None) -> bool:
    if as_json:
        catalogue.emit_json({**result, **(extra or {})})
        return result["ok"]
    if not result["ok"]:
        print(result["error"], flush=True)
        return False
    if result["outcome"] == "restarted":
        print(f"{result['label']}: applied; nodo restarted onto it.", flush=True)
    else:
        print(f"{result['label']}: applied. No node is serving, so the next start reads it.", flush=True)
    return True


# --- subcommands ------------------------------------------------------------

def config_get(args: List[str], as_json: bool, show_secrets: bool) -> bool:
    try:
        document = read_document()
    except (OSError, yaml.YAMLError) as e:
        return catalogue.emit_error(as_json, f"Could not read config.yaml: {e}")
    if args:
        try:
            segments = parse_path(args[0])
        except ValueError as e:
            return catalogue.emit_error(as_json, str(e))
        found, value = value_at(document, segments)
        if not found:
            return catalogue.emit_error(as_json, f"No such key: {args[0]}")
        path = format_path(segments)
    else:
        value, path = document, ""
    if not show_secrets:
        value = mask(value, path)
    if as_json:
        catalogue.emit_json({"path": path, "value": value})
        return True
    if isinstance(value, (dict, list)) and value:
        for leaf, leaf_value in flatten(value, parse_path(path) if path else []):
            print(f"{leaf}: {json.dumps(leaf_value, default=str)}")
    else:
        print(json.dumps(value, default=str))
    return True


def _assignments(args: List[str]) -> List[Tuple[List[Segment], str]]:
    pairs = []
    for argument in args:
        if "=" not in argument:
            raise ValueError(f"Expected <path>=<value>, got {argument!r}.")
        path, value = argument.split("=", 1)
        parse_value(value)  # refuse a malformed value before anything is written
        pairs.append((parse_path(path), value))
    return pairs


def config_set(args: List[str], as_json: bool) -> bool:
    if not args:
        return catalogue.emit_error(as_json, USAGE)
    try:
        pairs = _assignments(args)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    expression = " | ".join(f"{yq_path_expression(segments)} = env(NODO_CLI_V{index})"
                            for index, (segments, _) in enumerate(pairs))
    paths = [format_path(segments) for segments, _ in pairs]
    label = f"Set {', '.join(paths)}"
    result = apply_change(expression, [value for _, value in pairs], label)
    shown = {path: (MASK if is_secret_path(path) else parse_value(value))
             for path, (_, value) in zip(paths, pairs)}
    return _report(result, as_json, {"values": shown})


def config_append(args: List[str], as_json: bool) -> bool:
    if len(args) != 2:
        return catalogue.emit_error(as_json, USAGE)
    try:
        segments = parse_path(args[0])
        parse_value(args[1])
        found, current = value_at(read_document(), segments)
    except (ValueError, OSError, yaml.YAMLError) as e:
        return catalogue.emit_error(as_json, str(e))
    if not found or not isinstance(current, list):
        return catalogue.emit_error(as_json, f"{args[0]} is not a list in config.yaml.")
    result = apply_change(f"{yq_path_expression(segments)} += [env(NODO_CLI_V0)]",
                          [args[1]], f"Append to {format_path(segments)}")
    return _report(result, as_json)


def config_remove(args: List[str], as_json: bool) -> bool:
    if len(args) != 1:
        return catalogue.emit_error(as_json, USAGE)
    try:
        segments = parse_path(args[0])
        found, _ = value_at(read_document(), segments)
    except (ValueError, OSError, yaml.YAMLError) as e:
        return catalogue.emit_error(as_json, str(e))
    # As in the TUI, only a list *element* is removed: deleting a key would leave
    # the node falling back to a default nobody chose.
    if not isinstance(segments[-1], int):
        return catalogue.emit_error(
            as_json, "Only a list element can be removed, e.g. `service_networks.blacklist[0]`; "
                     "set a key to a new value instead.")
    if not found:
        return catalogue.emit_error(as_json, f"No such element: {args[0]}")
    result = apply_change(f"del({yq_path_expression(segments)})", [],
                          f"Remove {format_path(segments)}")
    return _report(result, as_json)


def profile_report(profile: Dict[str, Any], document: Any) -> Dict[str, Any]:
    """Which of the profile's keys differ from the file (the CELL page's ``report``)."""
    deviations = []
    for path, wanted_text in profile["writes"]:
        wanted = yaml.safe_load(wanted_text)
        found, current = value_at(document, parse_path(path))
        if found and _same(current, wanted):
            continue
        deviations.append({"path": path, "from": current if found else None,
                           "set": found, "to": wanted})
    return {"id": profile["id"], "label": profile["label"], "blurb": profile["blurb"],
            "total": len(profile["writes"]), "deviations": deviations}


def closest_profile(document: Any) -> Dict[str, Any]:
    """Fewest deviations; ties go to the earlier (more closed) profile."""
    reports = [profile_report(profile, document) for profile in PROFILES]
    return min(reports, key=lambda report: len(report["deviations"]))


def config_profile(args: List[str], as_json: bool, apply: bool) -> bool:
    try:
        document = read_document()
    except (OSError, yaml.YAMLError) as e:
        return catalogue.emit_error(as_json, f"Could not read config.yaml: {e}")
    if not args:
        if apply:
            return catalogue.emit_error(as_json, "Name the profile to apply: " +
                                        ", ".join(p["id"] for p in PROFILES))
        reports = [profile_report(profile, document) for profile in PROFILES]
        closest = closest_profile(document)["id"]
        if as_json:
            catalogue.emit_json({"closest": closest, "profiles": reports})
            return True
        for report in reports:
            marker = "*" if report["id"] == closest else " "
            count = len(report["deviations"])
            print(f"{marker} {report['id']:<12} {report['label']:<16} "
                  f"{'exact match' if not count else str(count) + ' deviation' + ('s' if count > 1 else '')}"
                  f"  -- {report['blurb']}")
        print("\n* closest. `nodo config profile <id>` lists the deviations; add --apply to adopt it.")
        return True

    profile = next((p for p in PROFILES if p["id"] == args[0]), None)
    if profile is None:
        return catalogue.emit_error(as_json, f"Unknown profile {args[0]!r}; one of: " +
                                    ", ".join(p["id"] for p in PROFILES))
    report = profile_report(profile, document)
    if not apply:
        if as_json:
            catalogue.emit_json({"profile": report})
            return True
        print(f"{report['label']}: {report['blurb']}")
        if not report["deviations"]:
            print("This node is exactly in this posture.")
        for deviation in report["deviations"]:
            current = json.dumps(deviation["from"]) if deviation["set"] else "(unset)"
            print(f"  {deviation['path']}: {current} -> {json.dumps(deviation['to'])}")
        return True

    if not report["deviations"]:
        result = {"ok": True, "label": f"Profile {profile['label']}", "outcome": "unchanged",
                  "backup": None, "error": None}
        if as_json:
            catalogue.emit_json(result)
        else:
            print(f"Already in {profile['label']}; nothing to write.")
        return True
    changes = report["deviations"]
    expression = " | ".join(
        f"{yq_path_expression(parse_path(change['path']))} = env(NODO_CLI_V{index})"
        for index, change in enumerate(changes))
    values = [dict(profile["writes"])[change["path"]] for change in changes]
    result = apply_change(expression, values, f"Profile {profile['label']}")
    return _report(result, as_json, {"changes": changes})


def config_command(argv=None) -> bool:
    """``nodo config <get|set|append|remove|profile> ...``."""
    argv = list(argv or [])
    as_json = "--json" in argv
    flags = {"--json", "--show-secrets", "--apply"}
    args = [a for a in argv if a not in flags]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE, flush=True)
        return bool(args)
    sub, rest = args[0], args[1:]
    if sub == "get":
        return config_get(rest, as_json, "--show-secrets" in argv)
    if sub == "set":
        return config_set(rest, as_json)
    if sub == "append":
        return config_append(rest, as_json)
    if sub == "remove":
        return config_remove(rest, as_json)
    if sub == "profile":
        return config_profile(rest, as_json, "--apply" in argv)
    return catalogue.emit_error(as_json, f"Unknown subcommand {sub!r}.\n{USAGE}")
