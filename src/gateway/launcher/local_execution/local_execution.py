import json
import traceback
from typing import Optional, Callable, List, Dict

import netifaces as ni

from protos import celaut_pb2 as celaut, celaut_pb2
from src.database.sql_connection import SQLConnection
from src.virtualizers.architecture import get_arch_tag
from src.virtualizers.interface import build, execute, get_configured_virtualizer
from src.virtualizers.selection import select_virtualizer
from src.manager.manager import (
    default_initial_balance,
    is_external_execute_client,
    reserve_instance_name,
)
from src.utils import utils, logger as log
from src.utils.instance_names import extract_instance_name
from src.utils.utils import from_amount
from src.utils.network import get_free_port
from src.utils.config import ConfigManager
from src.virtualizers.firewall import resolve_slot_transport_protocols


sc = SQLConnection()
env_manager = ConfigManager()


def _serialize_envs(config: Optional[celaut.Configuration]) -> str:
    """Serialize a Configuration's ``environment_variables`` map to JSON text.

    The instance is launched with these envs (see ``execute()``), so persisting them
    lets the node later tell how an instance was configured — e.g. whether a
    source-application was started as a seed signer (``SOURCE_SIGNER_MODE=seed``).
    Values are protobuf ``bytes``; they are decoded as UTF-8 (env vars are text).
    Returns ``None`` when there are no env vars, so the DB column stays NULL.
    """
    if config is None or not config.environment_variables:
        return None
    envs = {
        key: value.decode("utf-8", errors="replace")
        for key, value in config.environment_variables.items()
    }
    return json.dumps(envs, sort_keys=True) if envs else ""


_INTERFACE_PREFIX_PRIORITY = (
    "wl",
    "ww",
    "en",
    "eth",
)


def _interface_priority(interface: str) -> tuple[int, int, str]:
    normalized = (interface or "").strip().lower()
    if normalized in {"lo", "localhost"}:
        return (3, len(normalized), normalized)
    if utils.is_virtual_interface(normalized):
        return (2, len(normalized), normalized)
    if normalized.startswith(_INTERFACE_PREFIX_PRIORITY):
        return (0, len(normalized), normalized)
    return (1, len(normalized), normalized)


def _resolve_default_ipv4_interface() -> str:
    try:
        default_gateway = ni.gateways().get("default", {})
        default_route = default_gateway.get(ni.AF_INET)
        if default_route and len(default_route) > 1:
            return str(default_route[1])
    except Exception as e:
        log.LOGGER(f"Unable to resolve default IPv4 interface: {e}")
    return ""


def _resolve_default_ipv6_interface() -> str:
    try:
        default_gateway = ni.gateways().get("default", {})
        default_route = default_gateway.get(ni.AF_INET6)
        if default_route and len(default_route) > 1:
            return str(default_route[1])
    except Exception as e:
        log.LOGGER(f"Unable to resolve default IPv6 interface: {e}")
    return ""


def _find_any_host_interface_ip() -> str:
    for interface in sorted(ni.interfaces(), key=_interface_priority):
        if _interface_priority(interface)[0] >= 2:
            continue
        if interface in {"lo", "localhost"}:
            continue
        try:
            return utils.get_local_ip_from_network(network=interface, allow_link_local=False)
        except Exception:
            continue
    raise RuntimeError("Unable to find any host interface IP to advertise.")


def _get_external_advertised_host_ip(father_ip: str) -> str:
    configured_public_ip = str(env_manager.get("network.PUBLIC_IP", "") or "").strip()
    if configured_public_ip:
        return configured_public_ip

    configured_interface = str(env_manager.get("network.EXTERNAL_INTERFACE", "") or "").strip()
    if configured_interface:
        return utils.get_local_ip_from_network(network=configured_interface, allow_link_local=False)

    default_interface = _resolve_default_ipv4_interface()
    if default_interface:
        return utils.get_local_ip_from_network(network=default_interface, allow_link_local=False)

    default_ipv6_interface = _resolve_default_ipv6_interface()
    if default_ipv6_interface:
        return utils.get_local_ip_from_network(network=default_ipv6_interface, allow_link_local=False)

    try:
        return _find_any_host_interface_ip()
    except Exception as e:
        log.LOGGER(f"Unable to resolve host IP from available interfaces: {e}")

    if father_ip:
        resolved_network = utils.get_network_name(direction=father_ip)
        if resolved_network:
            return utils.get_local_ip_from_network(network=resolved_network, allow_link_local=False)

    raise RuntimeError(
        "Unable to resolve an external host IP to advertise. "
        "Configure network.PUBLIC_IP or network.EXTERNAL_INTERFACE."
    )

def local_execution(
        config: Optional[celaut_pb2.Configuration],
        resources: celaut_pb2.Service.Container.Resources,
        father_id: Optional[str],
        father_ip: Optional[str],
        metadata: celaut.Metadata,
        service: celaut.Service,
        service_id: Optional[str],
        refund_container: List[Callable]
) -> celaut_pb2.ServiceInstance:
    requested_instance_name, sanitized_config = extract_instance_name(config)
    config = sanitized_config or celaut_pb2.Configuration()
    instance_name = reserve_instance_name(requested_name=requested_instance_name)
    configured_virtualizer = get_configured_virtualizer()
    log.LOGGER(
        f"Local execution start: service_id={service_id}, father_id={father_id}, "
        f"father_ip={father_ip}, virtualizer={configured_virtualizer}, instance_name={instance_name}"
    )

    #  TODO check this.
    father_id = father_id if father_id else ""
    father_ip = father_ip if father_ip else ""

    # Resolved before the balance is derived, because it selects the memory price the
    # ticks that spend that balance will charge: funding an instance at the scalar
    # rate and then charging it a per-arch one buys a different number of hours than
    # `deposits.INITIAL_RUNTIME_HOURS` promises.
    service_arch = get_arch_tag(service=service, metadata=metadata)

    initial_mu: int = from_amount(config.initial_mu) \
        if config.HasField("initial_mu") \
        else default_initial_balance(
            system_resources=resources.at_init,
            service_hash=service_id,
            arch=service_arch,
        )

    # The initial end of the declared range. The virtualizer is handed the whole
    # `resources` (both ends): which of them it has to reserve at boot is the
    # backend's business, not the launcher's -- see `virtualizers.interface.execute`.
    initial_system_resources: celaut.Sysresources = resources.at_init

    try:
        service_id = build(
            service=service,
            metadata=metadata,
            service_id=service_id,
        )  # If the service is not built, build it.
    except Exception as e:
        try:
            log.LOGGER('Error building the service: ' + str(e))
            log.LOGGER(traceback.format_exc())
            refund_container.pop()()  # Give the charge back.
        except IndexError:
            log.LOGGER('Error refunding the charge.')
        finally:
            log.LOGGER(str(e))
            raise e
    log.LOGGER(f"Service build ready for execution: service_id={service_id}")

    father_is_local_vmachine = bool(father_id) and sc.internal_instance_exists(id=father_id)
    isolate_internal_children = env_manager.get("network.ISOLATE_INTERNAL_CHILDREN", True)
    is_dev_client = "dev" in father_id and env_manager.get("network.CONSIDER_DEV_AS_INTERNAL", True)
    disabled_outside = env_manager.get("network.DISABLE_EXPOSE_OUTSIDE", False)
    # `dev-external-` client ids are a synthetic pool used to exercise the
    # "exposed outside" code paths locally for testing, without a real remote
    # peer -- not a signal that the actual father is on another network (that
    # is what cross_network, below, is for).
    is_dev_forced_external = is_external_execute_client(father_id)
    # In case of dev instances, we consider them as internal.
    # If the father is internal, but isolate internal children is disabled, the child should be exposed outside.
    expose_outside: bool = not disabled_outside and (
        is_dev_forced_external
        or (not is_dev_client and (not father_is_local_vmachine or not isolate_internal_children))
    )
    if is_dev_forced_external and disabled_outside:
        log.LOGGER(
            "External exposure requested by configuration, but network.DISABLE_EXPOSE_OUTSIDE is enabled."
        )

    # Which of our own networks (if any) father_ip belongs to. None means father_ip
    # shares no subnet with any of our interfaces -- e.g. a real peer reached over
    # the internet -- which is a different situation from "not exposed" and must
    # not be resolved as if it were our own loopback (see get_network_name).
    resolved_network: Optional[str] = None
    if expose_outside and not is_dev_forced_external:
        resolved_network = utils.get_network_name(direction=father_ip)
    same_network = resolved_network is not None
    cross_network = expose_outside and not is_dev_forced_external and not same_network

    log.LOGGER(
        "Internal child isolation is "
        + ("enabled" if isolate_internal_children else "disabled")
        + (
            f" (father_id={father_id}, father_ip={father_ip}, by_local={not expose_outside}, "
            f"is_dev_forced_external={is_dev_forced_external}, cross_network={cross_network})"
        )
    )

    supported_slot_ports: List[int] = []
    for slot in service.api.slot:
        protocol = resolve_slot_transport_protocols(
            slot,
            logger_fn=log.LOGGER,
            context="[LOCAL_EXEC]",
        )
        if not protocol:
            log.LOGGER(
                f"[LOCAL_EXEC] Slot port={slot.port} ignored because it has no host-supported transports."
            )
            continue
        supported_slot_ports.append(slot.port)

    if not supported_slot_ports:
        log.LOGGER(
            "[LOCAL_EXEC] No host-supported API slots found. Service will be started without published URI slots."
        )

    free_port_ranges = env_manager.get("network.FREE_PORTS_RANGE", [])
    # father_ip is on a network of its own: the only address we could ever give it
    # is our own PUBLIC_IP, and that is only worth pairing with a port we believe
    # the operator's router forwards (network.FREE_PORTS_RANGE) -- an OS-assigned
    # ephemeral port is exactly as unreachable to father_ip as our LAN address
    # would be. Neither is auto-detected: the operator states both explicitly.
    configured_public_ip = ""
    if cross_network:
        configured_public_ip = str(env_manager.get("network.PUBLIC_IP", "") or "").strip()

    # Only allocate (and later DNAT) the slots we can actually route to from
    # outside. same_network and the dev-forced-external test path behave as
    # before: an OS ephemeral port when FREE_PORTS_RANGE is exhausted or unset is
    # still fine there, since it stays reachable on this node's own network.
    # cross_network with no PUBLIC_IP configured, or with every configured range
    # already taken, leaves the slot out entirely instead of failing the whole
    # launch: a tunnelled request reaches the container directly on its internal
    # address (src/tunneling/rpc_tunnel.py), bypassing this mapping, so nothing
    # needs to be opened on this host for a slot nobody will be told about.
    assigment_ports: Dict[int, int] = {}
    for port in supported_slot_ports:
        if not expose_outside:
            assigment_ports[port] = port
        elif cross_network:
            if not configured_public_ip or not free_port_ranges:
                continue
            try:
                assigment_ports[port] = get_free_port(free_port_ranges=free_port_ranges)
            except RuntimeError as e:
                log.LOGGER(f"[LOCAL_EXEC] {e} (slot {port}); leaving it unadvertised.")
        else:
            assigment_ports[port] = get_free_port(free_port_ranges=free_port_ranges)

    log.LOGGER(
        f"Execution network mode: by_local={not expose_outside}, "
        f"assigment_ports={assigment_ports}"
    )

    # The backend is chosen per service by architecture (native -> CH under KVM,
    # foreign-arch -> QEMU/TCG when emulation is enabled). Resolve it here from the
    # same `service` interface.execute() dispatches on, so the `virtualizer` column
    # persisted below matches the backend that actually runs -- lifecycle calls
    # (kill/maintain/firewall) route by that column.
    configured_virtualizer = select_virtualizer(service=service, metadata=metadata)

    # `service_arch` (resolved above, where the initial balance needed it) is
    # persisted on the row below. It selects the memory price when the operator has
    # set one per arch: the maintenance tick prices the *row*, and re-deriving the
    # arch there would mean reading the service off disk once per instance per tick --
    # for a service the instance may well outlive. None when the manifest names an
    # arch this node has no tag for, which is charged the scalar memory price rather
    # than nothing.
    log.LOGGER(
        f"Invoking virtualizer execute: virtualizer={configured_virtualizer}, "
        f"arch={service_arch}, service_id={service_id}, father_id={father_id}"
    )

    # A manifest that names no disk is rejected -- and it is rejected here, before a
    # VM is built and booted for it, rather than after.
    declared_disk_space = int(initial_system_resources.disk_space) \
        if initial_system_resources.HasField("disk_space") else 0
    if not declared_disk_space:
        raise Exception("Disk space is not specified in the system requirements range.")

    # The instance goes into the database while the guest is starting, not when the
    # launch finishes. A guest runs code -- and calls back into the node -- while
    # `execute` is still waiting for its network and applying its firewall rules,
    # and every node_controller call is attributed to its caller by source address.
    # An instance that is not on record yet is a caller the node cannot name, and
    # it used to answer that first call with
    # `Error charging for the resource change of <ip>`: the charge was simply where
    # the missing row surfaced first. The backend calls this the instant the guest
    # becomes able to speak; see `src/virtualizers/ch/execute.py`.
    #
    # `serialized_instance` is the one column that cannot be filled this early: the
    # published URI slots depend on the address the guest was given, which is an
    # argument to this callback. It is stored right after the launch returns.
    #
    # A backend calls this at most once -- `execute` launches exactly one VM per
    # call -- so what it needs to remember is "was it called, and with which id",
    # not a collection.
    registered_id: Optional[str] = None

    def _register_instance(
            vmachine_id: str,
            vmachine_ip: str,
            resolved_resources: celaut_pb2.Sysresources,
    ) -> None:
        nonlocal registered_id
        # Disk follows the resolved figure: the manifest has to declare it, but what
        # gets persisted is the size of the image the virtualizer actually handed the
        # instance, which is >= the declared one once the build's floors and mkfs
        # growth are applied. The manifest is only the fallback for a virtualizer
        # that does not report disk back.
        disk_space = int(resolved_resources.disk_space) or declared_disk_space
        if disk_space != declared_disk_space:
            log.LOGGER(
                f"Instance {vmachine_id} holds {disk_space} bytes of disk against a declared "
                f"{declared_disk_space}; billing the resolved figure."
            )
        # Every resource the instance holds, not just its disk: these columns are
        # what the maintenance tick prices it by, so a field left unrecorded is a
        # resource billed as zero for the instance's whole life. The compute and
        # memory figures come from `resolved_resources` -- what the virtualizer
        # actually reserved, floors applied -- never from a second, defaults-free
        # re-read of the manifest (#249).
        sc.add_local_instance(
            father_id=father_id,
            container_id=vmachine_id,
            name=instance_name,
            container_ip=vmachine_ip,
            balance_mu=initial_mu,
            serialized_instance=None,
            service_id=service_id,
            virtualizer=configured_virtualizer,
            disk_space=disk_space,
            envs=_serialize_envs(config),
            mem_limit=int(resolved_resources.mem_limit),
            cpu_period=int(resolved_resources.cpu_period),
            cpu_quota=int(resolved_resources.cpu_quota),
            arch=service_arch,
        )
        registered_id = vmachine_id

    # Execute virtualizer process.
    try:
        vmachine_id, vmachine_ip, resolved_resources = execute(
            assigment_ports=assigment_ports,
            by_local=not expose_outside,
            service_id=service_id,
            service=service,
            config=config,
            system_resources=resources,
            father_id=father_id,
            register_instance=_register_instance,
        )
    except Exception:
        # A launch that failed after the guest started leaves a row for an instance
        # that is not running: the backend has already torn the VM down, so the row
        # would be billed for a machine nobody can reach.
        if registered_id:
            log.LOGGER(f"Launch failed after registering {registered_id}; purging its row.")
            sc.purge_internal(id=registered_id)
        raise
    log.LOGGER(f"Virtualizer execute returned: vmachine_id={vmachine_id}, vmachine_ip={vmachine_ip}")

    # Resolve slots
    uri_slots: List[celaut.Instance.Uri_Slot] = []
    try:
        # get the host ip to be published for this instance. If the instance doesn't require to be exposed, publish the vmachine_ip, otherwise publish the local IP of this node.:
        if not expose_outside:
            _ip = vmachine_ip
        elif is_dev_forced_external:
            _ip = _get_external_advertised_host_ip(father_ip=father_ip)
        elif same_network:
            _ip = utils.get_local_ip_from_network(
                network=resolved_network,
                allow_link_local=False,
            )
        elif configured_public_ip:
            _ip = configured_public_ip
        else:
            _ip = None
            log.LOGGER(
                "[LOCAL_EXEC] father is on a different network and network.PUBLIC_IP is not "
                "configured; no address will be advertised -- the caller must use the service tunnel."
            )

        log.LOGGER(
            f"Preparing published URI slots: resolved_network={resolved_network}, "
            f"cross_network={cross_network}, advertised_ip={_ip or 'none'}, "
            f"father IP={father_ip if father_ip else 'N/A'}"
        )

        for internal in supported_slot_ports:
            uri_slot = celaut.Instance.Uri_Slot()
            uri_slot.internal_port = internal

            external = assigment_ports.get(internal)
            if _ip is not None and external is not None:
                uri_slot.uri.append(
                    celaut.Instance.Uri(
                        ip=_ip,
                        port=external
                    )
                )
                log.LOGGER(
                    f"Published URI mapping: internal_port={internal}, advertised={_ip}:{external}, "
                    f"vmachine_ip={vmachine_ip}, by_local={not expose_outside}"
                )
            else:
                log.LOGGER(
                    f"[LOCAL_EXEC] Slot {internal} has no advertisable address; the caller must tunnel it."
                )
            uri_slots.append(uri_slot)

    except Exception as e:
        log.LOGGER(f"Exception setting uri_slot: {str(e)}")
        log.LOGGER(traceback.format_exc())
        raise e

    instance = celaut.Instance(
            api=service.api,
            uri_slot=uri_slots
        )

    # The row itself was written while the guest was booting (`_register_instance`);
    # what is left is the definition, which needed the address the guest got.
    if registered_id != vmachine_id:
        # A backend that ignored the callback -- there is none today, but the
        # parameter is optional. Late is better than never, and it keeps one insert
        # in one place.
        log.LOGGER(
            f"Virtualizer did not register {vmachine_id} while it booted; recording it now."
        )
        _register_instance(vmachine_id, vmachine_ip, resolved_resources)
    sc.set_local_instance_definition(
        id=vmachine_id, serialized_instance=instance.SerializeToString()
    )
    log.LOGGER(
        f"Instance provisioned in DB: vmachine_id={vmachine_id}, virtualizer={configured_virtualizer}, "
        f"uri_slots={len(uri_slots)}"
    )
    
    return celaut_pb2.ServiceInstance(
        token=vmachine_id,
        instance=instance
    )
