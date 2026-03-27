from enum import Enum
from typing import Optional

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager

sc = SQLConnection()
env_manager = ConfigManager()


class TransportProtocol(Enum):
    """Supported network protocols."""
    TCP = "tcp"
    UDP = "udp"


def _normalize_virtualizer(name: Optional[str]) -> str:
    if not isinstance(name, str):
        return "docker"
    v = name.strip().lower()
    if v in {"ch", "cloud_hypervisor", "cloud-hypervisor"}:
        return "ch"
    if v == "docker":
        return "docker"
    return v


def _resolve_virtualizer(vmachine_id: str) -> str:
    try:
        virtualizer = sc.get_internal_virtualizer(id=vmachine_id)
        if isinstance(virtualizer, str) and virtualizer.strip():
            return _normalize_virtualizer(virtualizer)
    except Exception:
        pass
    return _normalize_virtualizer(env_manager.get("virtualizers.DEFAULT_VIRTUALIZER", "ch"))


def allow_connection(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import allow_connection as ch_allow_connection

        return ch_allow_connection(
            vmachine_id=vmachine_id,
            ip=ip,
            port=port,
            protocol=protocol,
        )

    from src.virtualizers.docker.firewall import allow_connection as docker_allow_connection

    return docker_allow_connection(
        container_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol,
    )


def allow_connection_to_instance(
    vmachine_id: str,
    instance: celaut.Instance,
) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import (
            allow_connection_to_instance as ch_allow_connection_to_instance,
        )

        return ch_allow_connection_to_instance(
            vmachine_id=vmachine_id,
            instance=instance,
        )

    from src.virtualizers.docker.firewall import allow_connection_to_instance as docker_allow_connection_to_instance

    return docker_allow_connection_to_instance(
        container_id=vmachine_id,
        instance=instance,
    )


def block_all(vmachine_id: str) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer == "ch":
        from src.virtualizers.ch.firewall import block_all as ch_block_all

        return ch_block_all(vmachine_id=vmachine_id)

    from src.virtualizers.docker.firewall import block_all as docker_block_all

    return docker_block_all(container_id=vmachine_id)


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
