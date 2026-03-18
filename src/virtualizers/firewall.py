from enum import Enum
from typing import Optional

from protos import celaut_pb2 as celaut


class TransportProtocol(Enum):
    """Supported network protocols."""
    TCP = "tcp"
    UDP = "udp"


def allow_connection(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
) -> bool:
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
    from src.virtualizers.docker.firewall import allow_connection_to_instance as docker_allow_connection_to_instance

    return docker_allow_connection_to_instance(
        container_id=vmachine_id,
        instance=instance,
    )


def block_all(vmachine_id: str) -> bool:
    from src.virtualizers.docker.firewall import block_all as docker_block_all

    return docker_block_all(container_id=vmachine_id)


def remove_rule(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
) -> bool:
    from src.virtualizers.docker.firewall import remove_rule as docker_remove_rule

    return docker_remove_rule(
        container_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol,
    )
