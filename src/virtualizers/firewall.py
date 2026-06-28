from enum import Enum
import shlex
import subprocess
from typing import Callable, List, Optional

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

sc = SQLConnection()
env_manager = ConfigManager()

FORWARD_RELATED_ESTABLISHED_COMMENT = "nodo;forward;related_established"
FORWARD_RELATED_ESTABLISHED_ARGS = [
    "-m",
    "conntrack",
    "--ctstate",
    "RELATED,ESTABLISHED",
    "-j",
    "ACCEPT",
    "-m",
    "comment",
    "--comment",
    FORWARD_RELATED_ESTABLISHED_COMMENT,
]


class TransportProtocol(Enum):
    """Supported network protocols."""

    TCP = "tcp"
    UDP = "udp"


SUPPORTED_TRANSPORT_TAGS = {
    TransportProtocol.TCP.value: TransportProtocol.TCP,
    TransportProtocol.UDP.value: TransportProtocol.UDP,
}


def serialize_transport_protocol(protocol: Optional[TransportProtocol]) -> bytes:
    if not protocol:
        return b""
    return str(protocol.value).strip().lower().encode("utf-8")


def resolve_slot_transport_protocols(
    slot: celaut.Service.Api.Slot,
    *,
    logger_fn: Callable[[str], None] = logger,
    context: str = "[FW]",
) -> Optional[TransportProtocol]:
    if slot is None:
        raise ValueError(f"{context} Slot is None.")

    if not slot.HasField("transport"):
        raise ValueError(
            f"{context} Slot port={slot.port} is missing required transport definition."
        )

    normalized_tags = [
        str(tag).strip().lower()
        for tag in slot.transport.tags
        if str(tag).strip()
    ]
    if not normalized_tags:
        raise ValueError(
            f"{context} Slot port={slot.port} has empty transport tags. At least one tag is required."
        )

    resolved: List[TransportProtocol] = []
    for tag in normalized_tags:
        protocol = SUPPORTED_TRANSPORT_TAGS.get(tag)
        if not protocol:
            logger_fn(
                f"{context} Slot port={slot.port} transport tag '{tag}' is unsupported by host. "
                "Supported tags: tcp, udp. Slot transport tag ignored."
            )
            continue
        if protocol not in resolved:
            resolved.append(protocol)

    if len(resolved) > 1:
        raise ValueError(
            f"{context} Slot port={slot.port} declares multiple transport families ({[p.value for p in resolved]}). "
            "Each slot must resolve to a single transport."
        )

    if not resolved:
        logger_fn(
            f"{context} Slot port={slot.port} has no host-supported transport tags. Slot will be ignored."
        )
        return None

    return resolved[0]


def _normalize_virtualizer(name: Optional[str]) -> str:
    if not isinstance(name, str):
        raise ValueError(f"Invalid virtualizer value: {name!r}")
    v = name.strip().lower()
    if not v:
        raise ValueError("Virtualizer value is empty.")
    if v in {"ch", "cloud_hypervisor", "cloud-hypervisor"}:
        return "ch"
    if v == "docker":
        return "docker"
    raise ValueError(f"Unknown virtualizer '{name}'. Supported: ch, docker.")


def _resolve_virtualizer(vmachine_id: str) -> str:
    try:
        virtualizer = sc.get_internal_virtualizer(id=vmachine_id)
        if isinstance(virtualizer, str) and virtualizer.strip():
            return _normalize_virtualizer(virtualizer)
    except Exception:
        pass
    default_virtualizer = env_manager.get("virtualizers.DEFAULT_VIRTUALIZER", "ch")
    return _normalize_virtualizer(default_virtualizer)


def _run_iptables(command: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["iptables"] + command,
        capture_output=True,
        text=True,
        check=check,
    )


def _option_value(tokens: List[str], option: str) -> Optional[str]:
    for index in range(len(tokens) - 1):
        if tokens[index] == option:
            return tokens[index + 1]
    return None


def _is_nodo_related_established_rule(tokens: List[str]) -> bool:
    if len(tokens) < 3 or tokens[1] != "FORWARD":
        return False

    ctstate = _option_value(tokens, "--ctstate")
    jump = _option_value(tokens, "-j")
    comment = _option_value(tokens, "--comment") or ""

    return (
        ctstate == "RELATED,ESTABLISHED"
        and jump == "ACCEPT"
        and comment.startswith("nodo")
    )


def _is_exact_related_established_rule(tokens: List[str]) -> bool:
    return _is_nodo_related_established_rule(tokens) and (
        (_option_value(tokens, "--comment") == FORWARD_RELATED_ESTABLISHED_COMMENT)
    )


def ensure_forward_related_established_rule() -> bool:
    """
    Ensure canonical RELATED,ESTABLISHED rule exists at top of FORWARD chain.

    Canonical rule:
    iptables -I FORWARD 1 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
      -m comment --comment "nodo;forward;related_established"
    """
    try:
        listed = _run_iptables(["-S", "FORWARD"], check=True)
        lines = [line.strip() for line in (listed.stdout or "").splitlines() if line.strip()]

        forward_rules: List[List[str]] = []
        for line in lines:
            if not line.startswith("-A FORWARD"):
                continue
            try:
                forward_rules.append(shlex.split(line))
            except ValueError:
                logger(f"[FW] Unable to parse FORWARD rule: {line}")

        matching = [rule for rule in forward_rules if _is_nodo_related_established_rule(rule)]

        if (
            len(matching) == 1
            and len(forward_rules) > 0
            and forward_rules[0] == matching[0]
            and _is_exact_related_established_rule(matching[0])
        ):
            return True

        # Remove existing nodo RELATED,ESTABLISHED entries first, then reinsert canonical at top.
        for rule in reversed(matching):
            delete_rule = rule.copy()
            delete_rule[0] = "-D"
            removed = _run_iptables(delete_rule, check=False)
            if removed.returncode != 0:
                logger(
                    "[FW] Failed removing duplicate RELATED,ESTABLISHED rule: "
                    f"{(removed.stderr or removed.stdout or '').strip()}"
                )

        inserted = _run_iptables(
            ["-I", "FORWARD", "1", *FORWARD_RELATED_ESTABLISHED_ARGS],
            check=True,
        )
        if inserted.returncode == 0:
            logger("[FW] Ensured global FORWARD RELATED,ESTABLISHED rule in position 1.")
        return inserted.returncode == 0

    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip() or (e.stdout or "").strip() or str(e)
        logger(f"[FW] Failed ensuring global FORWARD RELATED,ESTABLISHED rule: {stderr}")
        return False
    except Exception as e:
        logger(f"[FW] Unexpected error ensuring global FORWARD RELATED,ESTABLISHED rule: {e}")
        return False


def allow_connection(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import allow_connection as ch_allow_connection

        return ch_allow_connection(
            vmachine_id=vmachine_id,
            ip=ip,
            port=port,
            protocol=protocol,
            source_ip=source_ip,
        )

    if virtualizer == "docker":
        from src.virtualizers.docker.firewall import allow_connection as docker_allow_connection

        return docker_allow_connection(
            container_id=vmachine_id,
            ip=ip,
            port=port,
            protocol=protocol,
        )

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def allow_connection_to_instance(
    vmachine_id: str,
    instance: celaut.Instance,
    source_ip: Optional[str] = None,
) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import (
            allow_connection_to_instance as ch_allow_connection_to_instance,
        )

        return ch_allow_connection_to_instance(
            vmachine_id=vmachine_id,
            instance=instance,
            source_ip=source_ip,
        )

    if virtualizer == "docker":
        from src.virtualizers.docker.firewall import (
            allow_connection_to_instance as docker_allow_connection_to_instance,
        )

        return docker_allow_connection_to_instance(
            container_id=vmachine_id,
            instance=instance,
        )

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def block_all(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import block_all as ch_block_all

        return ch_block_all(vmachine_id=vmachine_id, source_ip=source_ip)

    if virtualizer == "docker":
        from src.virtualizers.docker.firewall import block_all as docker_block_all

        return docker_block_all(container_id=vmachine_id)

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def allow_all_egress(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import allow_all_egress as ch_allow_all_egress

        return ch_allow_all_egress(vmachine_id=vmachine_id, source_ip=source_ip)

    if virtualizer == "docker":
        from src.virtualizers.docker.firewall import allow_all_egress as docker_allow_all_egress

        return docker_allow_all_egress(container_id=vmachine_id)

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def remove_rule(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import remove_rule as ch_remove_rule

        return ch_remove_rule(
            vmachine_id=vmachine_id,
            ip=ip,
            port=port,
            protocol=protocol,
        )

    if virtualizer == "docker":
        from src.virtualizers.docker.firewall import remove_rule as docker_remove_rule

        return docker_remove_rule(
            container_id=vmachine_id,
            ip=ip,
            port=port,
            protocol=protocol,
        )

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")
