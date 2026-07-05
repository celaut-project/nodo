import json
import os
from typing import Dict, List, Optional, Tuple

import docker as docker_lib

from protos import celaut_pb2 as celaut

from src.utils import logger as log
from src.utils.runtime import DOCKER_CLIENT
from src.utils.config import ConfigManager
from src.virtualizers.entry_path import resolve_entrypoint_path

from src.virtualizers.firewall import (
    TransportProtocol,
    allow_connection,
    allow_connection_to_instance,
    block_all,
    resolve_slot_transport_protocols,
)
from src.gateway.utils import GATEWAY_PORT
from src.manager.networks import filter_networks_with_ancestors, resolve_network
from src.virtualizers.docker.set_container_config import set_config
from src.database.sql_connection import SQLConnection

env_manager = ConfigManager()
_missing_seccomp_profile_warned = False


def _is_apparmor_enabled() -> bool:
    try:
        with open("/sys/module/apparmor/parameters/enabled", "r", encoding="utf-8") as f:
            return f.read().strip().lower().startswith("y")
    except OSError:
        return False


def _is_selinux_enabled() -> bool:
    try:
        with open("/sys/fs/selinux/enforce", "r", encoding="utf-8") as f:
            return f.read().strip() in {"0", "1"}
    except OSError:
        return False


def _default_seccomp_profile_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "seccomp.json"))


def __get_container_security_opts() -> List[str]:
    global _missing_seccomp_profile_warned

    security_opts: List[str] = []
    seccomp_profile_path = _default_seccomp_profile_path()

    if no_seccomp := env_manager.get("virtualizers.docker.SECURITY_NO_SECCOMP", False):
        log.LOGGER("Running container without seccomp profile due to configuration.")
        security_opts.append("seccomp=unconfined")
    else:
        if seccomp_profile_path:
            if os.path.isfile(seccomp_profile_path):
                with open(seccomp_profile_path, "r", encoding="utf-8") as f:
                    seccomp_content = json.dumps(json.load(f), separators=(",", ":"))
                security_opts.append(f"seccomp={seccomp_content}")
            elif not _missing_seccomp_profile_warned:
                log.LOGGER(
                    f"Missing seccomp profile at {seccomp_profile_path}. "
                    f"Using Docker default seccomp profile."
                )
                _missing_seccomp_profile_warned = True

    if env_manager.get("virtualizers.docker.SECURITY_APPARMOR_UNCONFINED", True) and _is_apparmor_enabled():
        security_opts.append("apparmor=unconfined")

    if env_manager.get("virtualizers.docker.SECURITY_SELINUX_DISABLE_LABEL", True) and _is_selinux_enabled():
        security_opts.append("label=disable")

    return security_opts


def create_container(id: str, entrypoint: str, use_other_ports=None) -> docker_lib.models.containers.Container:
    try:
        create_args = {
            "image": id + '.docker',  # https://github.com/moby/moby/issues/20972#issuecomment-193381422
            "entrypoint": entrypoint,
            "ports": use_other_ports,
            "dns": ["127.0.0.1"]
        }

        security_opts = __get_container_security_opts()
        if security_opts:
            create_args["security_opt"] = security_opts

        if env_manager.get("virtualizers.docker.SECURITY_PRIVILEGED", False):
            log.LOGGER("🚨 Running container in PRIVILEGED mode (full host access) due to configuration.")
            create_args["privileged"] = True

        return DOCKER_CLIENT().containers.create(
            **create_args
        )
    except docker_lib.errors.ImageNotFound as e:
        log.LOGGER('CONTAINER IMAGE NOT FOUND')
        # TODO build(id) using agents model.
        raise e
    except Exception as e:
        log.LOGGER('DOCKER RUN ERROR -> ' + str(e))
        raise e


def _build_docker_port_bindings(
    service: celaut.Service,
    assigment_ports: Optional[Dict[int, int]],
) -> Dict[str, int]:
    if not assigment_ports:
        return {}

    slot_by_port = {slot.port: slot for slot in service.api.slot}
    docker_ports: Dict[str, int] = {}

    for internal_port, external_port in assigment_ports.items():
        slot = slot_by_port.get(internal_port)
        if not slot:
            log.LOGGER(
                f"[DOCKER] Internal port {internal_port} has no API slot definition. Skipping published port mapping."
            )
            continue

        protocol = resolve_slot_transport_protocols(
            slot,
            logger_fn=log.LOGGER,
            context="[DOCKER]",
        )
        if not protocol:
            log.LOGGER(
                f"[DOCKER] Internal port {internal_port} has no host-supported transport tags. "
                "Skipping published port mapping."
            )
            continue

        docker_ports[f"{internal_port}/{protocol.value}"] = external_port

    return docker_ports


def execute(assigment_ports, by_local, service_id, service, config, initial_system_resources, father_id) -> Tuple[str, str]:
    entry_path = list(service.container.init.entry_path)
    resolved_entrypoint = resolve_entrypoint_path(entry_path=entry_path)
    published_ports = _build_docker_port_bindings(service=service, assigment_ports=assigment_ports)
    container = create_container(
        use_other_ports=published_ports if not by_local and published_ports else None,
        id=service_id,
        entrypoint=resolved_entrypoint
    )

    networks = service.network

    #  Filter networks if ancestors do not explicitly allow them.
    if SQLConnection().internal_instance_exists(id=father_id):
        networks = filter_networks_with_ancestors(networks=networks, father_id=father_id)

    # The requesting instance's own environment values drive Network peer
    # filtering (Service.Network.environment_variable).
    requester_env_values = dict(config.environment_variables) if config else None

    # Obtain instances to connect to the available networks.
    networks_resolved: List[celaut.ConfigurationFile.NetworkResolution] = [
        celaut.ConfigurationFile.NetworkResolution(
            tags=network.tags,
            peer_instances=resolve_network(network, requester_env_values=requester_env_values)
        )
        for network in networks if len(network.tags) > 0
    ]

    #  Set the configuration file into the instance file system root.
    set_config(
        container_id=container.id, 
        config=config, 
        resources=initial_system_resources,
        api=service.container.config_declaration,
        network_resolution=networks_resolved
    )

    # The container must be started after adding the configuration file and
    #  before requiring its IP address, since docker assigns it at startup.

    try:
        container.start()
    except docker_lib.errors.APIError as e:
        log.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(e))
        raise e

    # Reload this object from the server again and update attrs with the new data.
    container.reload()

    if not block_all(vmachine_id=container.id):
        log.LOGGER(f"Docker firewall block all function failed for {container.id}")

    # Allow connection to the node gateway.
    if not allow_connection(
        vmachine_id=container.id,
        ip='172.17.0.1', port=GATEWAY_PORT, # Gateway internal endpoint.
        protocol=TransportProtocol.TCP # Gateway communication is with TCP
    ):
        log.LOGGER(f"Docker firewall allow connection function failed for {container.id}")

    for network_resolution in networks_resolved:
        tag = network_resolution.tags[0]

        for instance in network_resolution.peer_instances:
            if allow_connection_to_instance(vmachine_id=container.id, instance=instance):
                log.LOGGER(f"Container {container.id} allowed to connect with {tag}.")
                break
            else:
                log.LOGGER(f"Container {container.id} not allowed to connect with {tag}!  This will cause errors.")  # TODO. Control that.

    return container.id, container.attrs['NetworkSettings']['IPAddress']
