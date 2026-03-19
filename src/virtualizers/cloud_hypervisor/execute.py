import hashlib
import ipaddress
import json
import math
import os
import posixpath
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.gateway.utils import generate_node_peer_info
from src.manager.networks import filter_networks_with_ancestors, resolve_network
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.architecture import UnsupportedArchitectureException, get_arch_tag
from src.virtualizers.cloud_hypervisor.runtime_state import save_runtime_state, delete_runtime_state, list_runtime_states

env_manager = ConfigManager()
sc = SQLConnection()

CACHE = env_manager.get("CACHE")
CH_BINARY_PATH = env_manager.get("virtualizers.cloud_hypervisor.BINARY_PATH")
NETWORK_MODE = env_manager.get("virtualizers.cloud_hypervisor.NETWORK_MODE", "tap_bridge")
NETWORK_BRIDGE_NAME = env_manager.get("virtualizers.cloud_hypervisor.NETWORK_BRIDGE_NAME", "br-ch")
NETWORK_SUBNET = env_manager.get("virtualizers.cloud_hypervisor.NETWORK_SUBNET", "192.168.200.0/24")
NETWORK_GATEWAY_IP = env_manager.get("virtualizers.cloud_hypervisor.NETWORK_GATEWAY_IP", "192.168.200.1")
GUEST_NET_DEVICE = env_manager.get("virtualizers.cloud_hypervisor.GUEST_NET_DEVICE", "auto")
KERNEL_CMDLINE_EXTRA = env_manager.get("virtualizers.cloud_hypervisor.KERNEL_CMDLINE_EXTRA", "console=ttyS0")
CH_SERIAL_MODE = env_manager.get("virtualizers.cloud_hypervisor.SERIAL_MODE", "file")
CH_CONSOLE_MODE = env_manager.get("virtualizers.cloud_hypervisor.CONSOLE_MODE", "off")
GUEST_NETWORK_READY_TIMEOUT_S = env_manager.get(
    "virtualizers.cloud_hypervisor.GUEST_NETWORK_READY_TIMEOUT_S",
    8,
)

DEFAULT_VCPUS = 1
DEFAULT_MEM_MIB = 256
MIN_MEM_MIB = 128


class CHExecuteError(RuntimeError):
    pass


def _run(command: List[str], *, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=True,
        )
    except FileNotFoundError as e:
        raise CHExecuteError(f"Required command not found: {command[0]}") from e
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        stdout = e.stdout.strip() if e.stdout else ""
        details: List[str] = []
        if stdout:
            details.append(f"stdout={stdout}")
        if stderr:
            details.append(f"stderr={stderr}")
        raise CHExecuteError(
            f"Command failed ({e.returncode}): {' '.join(command)} -> "
            f"{' | '.join(details) if details else 'unknown error'}"
        ) from e


def _bundle_dir(service_id: str, arch: str) -> Path:
    if not CACHE:
        raise CHExecuteError("CACHE path is not configured.")
    return Path(CACHE) / "cloud_hypervisor" / service_id / arch


def _runtime_vm_dir(vmachine_id: str) -> Path:
    if not CACHE:
        raise CHExecuteError("CACHE path is not configured.")
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _resolve_ch_binary() -> str:
    if CH_BINARY_PATH:
        if os.path.isfile(CH_BINARY_PATH) and os.access(CH_BINARY_PATH, os.X_OK):
            return CH_BINARY_PATH
        raise CHExecuteError(
            f"Configured cloud-hypervisor binary is invalid or not executable: {CH_BINARY_PATH}"
        )

    resolved = shutil.which("cloud-hypervisor")
    if not resolved:
        raise CHExecuteError(
            "cloud-hypervisor binary not found. Set virtualizers.cloud_hypervisor.BINARY_PATH or install it in PATH."
        )
    return resolved


def _resolve_service_arch(service_id: str, service: celaut.Service) -> str:
    arch = get_arch_tag(service=service, metadata=None)
    if arch:
        return arch

    base_dir = Path(CACHE) / "cloud_hypervisor" / service_id if CACHE else None
    if base_dir and base_dir.is_dir():
        candidates = [p.name for p in base_dir.iterdir() if p.is_dir() and (p / "bundle.json").is_file()]
        if len(candidates) == 1:
            return candidates[0]

    raise UnsupportedArchitectureException(arch="unknown")


def _load_bundle(service_id: str, arch: str) -> Dict[str, str]:
    bundle_dir = _bundle_dir(service_id, arch)
    bundle_path = bundle_dir / "bundle.json"
    if not bundle_path.is_file():
        raise CHExecuteError(f"Missing CH bundle manifest: {bundle_path}")

    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    rootfs_path = Path(bundle.get("rootfs_path", ""))
    kernel_path = Path(bundle.get("kernel_path", ""))
    initramfs_path = Path(bundle.get("initramfs_path", ""))

    if not rootfs_path.is_file():
        raise CHExecuteError(f"Missing CH rootfs image: {rootfs_path}")
    if not kernel_path.is_file():
        raise CHExecuteError(f"Missing CH kernel image: {kernel_path}")
    if not initramfs_path.is_file():
        raise CHExecuteError(f"Missing CH initramfs image: {initramfs_path}")

    return {
        "rootfs_path": str(rootfs_path),
        "kernel_path": str(kernel_path),
        "initramfs_path": str(initramfs_path),
        "arch": bundle.get("arch", arch),
    }


def _validate_custom_initramfs(initramfs_path: str) -> None:
    _ensure_command_available("lsinitramfs")
    result = _run(["lsinitramfs", initramfs_path], check=False)
    if result.returncode != 0:
        raise CHExecuteError(
            f"Unable to inspect initramfs with lsinitramfs: {initramfs_path}. "
            f"stderr={((result.stderr or '').strip() or '<empty>')}"
        )

    entries = {
        line.strip().lstrip("./")
        for line in (result.stdout or "").splitlines()
        if line.strip()
    }
    required_entries = {
        "init",
        "bin/busybox",
        "etc/nodo-ch-initramfs.marker",
    }
    missing = sorted(required_entries.difference(entries))
    if missing:
        raise CHExecuteError(
            "Invalid Cloud Hypervisor initramfs. Missing required custom entries: "
            f"{missing}. initramfs={initramfs_path}. Re-run installation to regenerate "
            "the custom CH initramfs."
        )


def _validate_entrypoint_strict(service: celaut.Service) -> str:
    raw_entrypoints = [str(item).strip() for item in service.container.entrypoint]
    entrypoints = [item for item in raw_entrypoints if item]

    if len(entrypoints) != 1:
        raise CHExecuteError(
            "Cloud Hypervisor requires exactly one entrypoint path. "
            f"Received {len(entrypoints)} value(s): {raw_entrypoints}"
        )

    entrypoint = entrypoints[0]
    if not entrypoint.startswith("/"):
        raise CHExecuteError(
            f"Cloud Hypervisor entrypoint must be an absolute path. Got: '{entrypoint}'."
        )
    if any(char.isspace() for char in entrypoint):
        raise CHExecuteError(
            "Cloud Hypervisor entrypoint must be a single executable path without spaces. "
            f"Got: '{entrypoint}'."
        )

    return entrypoint


def _guest_network_ready_timeout_seconds() -> float:
    raw_timeout = GUEST_NETWORK_READY_TIMEOUT_S
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as e:
        raise CHExecuteError(
            f"Invalid GUEST_NETWORK_READY_TIMEOUT_S value: {raw_timeout!r}"
        ) from e

    if timeout <= 0:
        raise CHExecuteError(
            f"GUEST_NETWORK_READY_TIMEOUT_S must be > 0. Got: {raw_timeout!r}"
        )
    return timeout


def _ip_network() -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(NETWORK_SUBNET, strict=False)
    except ValueError as e:
        raise CHExecuteError(f"Invalid NETWORK_SUBNET: {NETWORK_SUBNET}") from e

    if network.version != 4:
        raise CHExecuteError("Only IPv4 subnets are supported for Cloud Hypervisor networking.")
    if network.num_addresses < 4:
        raise CHExecuteError(f"NETWORK_SUBNET too small for VM allocation: {NETWORK_SUBNET}")
    return network


def _used_ips() -> set[str]:
    used: set[str] = set()

    for state in list_runtime_states().values():
        ip = state.get("ip")
        if isinstance(ip, str) and ip:
            used.add(ip)

    for vmachine_id in sc.get_all_internal_containers_ids():
        ip = sc.get_internal_ip(id=vmachine_id)
        if ip:
            used.add(ip)

    return used


def _deterministic_ip_and_mac(vmachine_id: str) -> Tuple[str, str]:
    network = _ip_network()
    gateway_ip = ipaddress.ip_address(NETWORK_GATEWAY_IP)
    if gateway_ip not in network:
        raise CHExecuteError(
            f"NETWORK_GATEWAY_IP {NETWORK_GATEWAY_IP} does not belong to subnet {NETWORK_SUBNET}"
        )

    hosts = network.num_addresses - 2
    if hosts <= 1:
        raise CHExecuteError(f"No usable host addresses in subnet {NETWORK_SUBNET}")

    used = _used_ips()
    digest = hashlib.sha256(vmachine_id.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")

    selected_ip: Optional[str] = None
    for offset in range(hosts):
        host_index = ((seed + offset) % hosts) + 1
        candidate = ipaddress.ip_address(int(network.network_address) + host_index)
        candidate_str = str(candidate)
        if candidate == gateway_ip:
            continue
        if candidate_str in used:
            continue
        selected_ip = candidate_str
        break

    if not selected_ip:
        raise CHExecuteError(f"No available IPs in subnet {NETWORK_SUBNET}")

    mac = f"02:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}:{digest[3]:02x}:{digest[4]:02x}"
    return selected_ip, mac


def _ensure_command_available(command: str) -> None:
    if not shutil.which(command):
        raise CHExecuteError(f"Required command not found in PATH: {command}")


def _network_preflight() -> ipaddress.IPv4Network:
    if NETWORK_MODE != "tap_bridge":
        raise CHExecuteError(
            f"Unsupported NETWORK_MODE '{NETWORK_MODE}'. This phase supports only 'tap_bridge'."
        )

    for command in ("ip", "sysctl", "iptables", "debugfs", "ping"):
        _ensure_command_available(command)

    network = _ip_network()
    prefix_len = network.prefixlen

    link_exists = _run(["ip", "link", "show", NETWORK_BRIDGE_NAME], check=False)
    if link_exists.returncode != 0:
        _run(["ip", "link", "add", NETWORK_BRIDGE_NAME, "type", "bridge"])

    addr_show = _run(["ip", "-4", "addr", "show", "dev", NETWORK_BRIDGE_NAME], check=False)
    expected_cidr = f"{NETWORK_GATEWAY_IP}/{prefix_len}"
    if expected_cidr not in (addr_show.stdout or ""):
        _run(["ip", "addr", "add", expected_cidr, "dev", NETWORK_BRIDGE_NAME])

    _run(["ip", "link", "set", NETWORK_BRIDGE_NAME, "up"])
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])

    return network


def _create_tap(vmachine_id: str) -> str:
    tap_suffix = hashlib.sha1(vmachine_id.encode("utf-8")).hexdigest()[:10]
    tap_name = f"tap{tap_suffix}"

    _run(["ip", "tuntap", "add", "dev", tap_name, "mode", "tap"])
    _run(["ip", "link", "set", tap_name, "master", NETWORK_BRIDGE_NAME])
    _run(["ip", "link", "set", tap_name, "up"])

    return tap_name


def _delete_tap(tap_name: str) -> None:
    _run(["ip", "link", "del", tap_name], check=False)


def _resolve_initial_resources(resources: celaut.Sysresources) -> Tuple[int, int]:
    vcpus = DEFAULT_VCPUS
    mem_mib = DEFAULT_MEM_MIB

    try:
        if resources and resources.HasField("cpu_quota") and resources.HasField("cpu_period"):
            if resources.cpu_period > 0 and resources.cpu_quota > 0:
                vcpus = max(1, int(math.ceil(resources.cpu_quota / resources.cpu_period)))

        if resources and resources.HasField("mem_limit") and resources.mem_limit > 0:
            mem_mib = max(MIN_MEM_MIB, int(math.ceil(resources.mem_limit / (1024 * 1024))))
    except Exception:
        pass

    return vcpus, mem_mib


def _build_network_resolution(service: celaut.Service, father_id: str) -> List[celaut.ConfigurationFile.NetworkResolution]:
    networks = service.network
    if father_id and sc.internal_instance_exists(id=father_id):
        networks = filter_networks_with_ancestors(networks=networks, father_id=father_id)

    return [
        celaut.ConfigurationFile.NetworkResolution(
            tags=network.tags,
            peer_instances=resolve_network(network),
        )
        for network in networks
        if len(network.tags) > 0
    ]


def _build_configuration_file(
    config: Optional[celaut.Configuration],
    resources: celaut.Sysresources,
    network_resolution: List[celaut.ConfigurationFile.NetworkResolution],
) -> celaut.ConfigurationFile:
    cfg = celaut.ConfigurationFile()
    local_peer = generate_node_peer_info(network=NETWORK_BRIDGE_NAME)
    cfg.gateway.CopyFrom(local_peer.instance)

    if config:
        cfg.config.CopyFrom(config)

    if network_resolution:
        cfg.network_resolution.extend(network_resolution)

    if resources:
        cfg.initial_sysresources.CopyFrom(resources)

    return cfg


def _run_debugfs_write(image_path: Path, host_file: Path, guest_target: str) -> None:
    guest_target = guest_target if guest_target.startswith("/") else f"/{guest_target}"
    target_dir = posixpath.dirname(guest_target)

    directory_parts = [part for part in target_dir.split("/") if part]
    current = ""
    for part in directory_parts:
        current = f"{current}/{part}"
        mkdir_result = _run(
            ["debugfs", "-w", "-R", f"mkdir {current}", str(image_path)],
            check=False,
        )
        if mkdir_result.returncode != 0:
            stderr = (mkdir_result.stderr or "").strip().lower()
            if "file exists" not in stderr:
                raise CHExecuteError(
                    f"debugfs mkdir failed for {current}: {mkdir_result.stderr or mkdir_result.stdout or ''}"
                )

    _run(["debugfs", "-w", "-R", f"rm {guest_target}", str(image_path)], check=False)

    write_cmd = f"write {host_file} {guest_target}"
    _run(["debugfs", "-w", "-R", write_cmd, str(image_path)])


def _add_dnat_rule(protocol: str, external_port: int, vm_ip: str, internal_port: int) -> List[List[str]]:
    add_commands = [
        ["iptables", "-t", "nat", "-A", "PREROUTING", "-p", protocol, "--dport", str(external_port), "-j", "DNAT", "--to-destination", f"{vm_ip}:{internal_port}"],
        ["iptables", "-t", "nat", "-A", "OUTPUT", "-p", protocol, "--dport", str(external_port), "-j", "DNAT", "--to-destination", f"{vm_ip}:{internal_port}"],
        ["iptables", "-A", "FORWARD", "-p", protocol, "-d", vm_ip, "--dport", str(internal_port), "-j", "ACCEPT"],
    ]

    for command in add_commands:
        _run(command)

    return [
        ["iptables", "-t", "nat", "-D", "PREROUTING", "-p", protocol, "--dport", str(external_port), "-j", "DNAT", "--to-destination", f"{vm_ip}:{internal_port}"],
        ["iptables", "-t", "nat", "-D", "OUTPUT", "-p", protocol, "--dport", str(external_port), "-j", "DNAT", "--to-destination", f"{vm_ip}:{internal_port}"],
        ["iptables", "-D", "FORWARD", "-p", protocol, "-d", vm_ip, "--dport", str(internal_port), "-j", "ACCEPT"],
    ]


def _remove_rules(commands: List[List[str]]) -> None:
    for command in commands:
        _run(command, check=False)


def _resolve_ch_stream_args(runtime_dir: Path) -> Tuple[List[str], Optional[Path]]:
    args: List[str] = []
    serial_log_path: Optional[Path] = None

    serial_mode = str(CH_SERIAL_MODE).strip() if CH_SERIAL_MODE is not None else ""
    serial_mode_lower = serial_mode.lower()
    if serial_mode:
        if serial_mode_lower == "file":
            serial_log_path = runtime_dir / "cloud-hypervisor.serial.log"
            args.extend(["--serial", f"file={serial_log_path}"])
        elif serial_mode_lower in {"off", "null", "tty"}:
            args.extend(["--serial", serial_mode_lower])
        else:
            args.extend(["--serial", serial_mode])
            if serial_mode_lower.startswith("file="):
                serial_path_value = serial_mode.split("=", 1)[1].strip()
                if serial_path_value:
                    serial_log_path = Path(serial_path_value)

    console_mode = str(CH_CONSOLE_MODE).strip() if CH_CONSOLE_MODE is not None else ""
    console_mode_lower = console_mode.lower()
    if console_mode:
        if console_mode_lower in {"off", "null", "tty"} or console_mode_lower.startswith("file="):
            args.extend(["--console", console_mode])
        else:
            raise CHExecuteError(
                f"Invalid CONSOLE_MODE value '{console_mode}'. Expected one of off/null/tty/file=<path>."
            )

    return args, serial_log_path


def _log_host_network_probe(vmachine_id: str, vm_ip: str, tap_name: Optional[str]) -> None:
    probes: List[Tuple[str, List[str]]] = [
        ("bridge_addr", ["ip", "-4", "addr", "show", "dev", NETWORK_BRIDGE_NAME]),
        ("route_to_vm", ["ip", "-4", "route", "get", vm_ip]),
        ("neigh_vm", ["ip", "-4", "neigh", "show", vm_ip]),
    ]
    if tap_name:
        probes.append(("tap_link", ["ip", "link", "show", tap_name]))

    for label, command in probes:
        result = _run(command, check=False)
        stdout = (result.stdout or "").strip() or "<empty>"
        stderr = (result.stderr or "").strip() or "<empty>"
        log.LOGGER(
            f"[CH][{vmachine_id}] host network probe {label}: rc={result.returncode}, "
            f"stdout={stdout}, stderr={stderr}"
        )


def _neighbor_is_usable(neigh_output: str) -> bool:
    text = neigh_output.strip().upper()
    if not text:
        return False
    if "FAILED" in text or "INCOMPLETE" in text:
        return False
    if "REACHABLE" in text or "STALE" in text or "DELAY" in text or "PROBE" in text or "PERMANENT" in text:
        return True
    return "LLADDR" in text


def _detect_initramfs_fatal(serial_log_path: Optional[Path]) -> Optional[str]:
    if not serial_log_path:
        return None
    serial_tail = _tail_file(serial_log_path, max_lines=200)
    if not serial_tail or serial_tail.startswith("<"):
        return None

    marker = "[nodo-ch-initramfs] ERROR:"
    fatal_line: Optional[str] = None
    for line in serial_tail.splitlines():
        if marker in line:
            fatal_line = line.strip()
    return fatal_line


def _wait_guest_network_ready(
    vmachine_id: str,
    vm_ip: str,
    timeout_s: float,
    serial_log_path: Optional[Path],
) -> None:
    deadline = time.monotonic() + timeout_s
    attempt = 0
    last_ping_stdout = "<empty>"
    last_ping_stderr = "<empty>"
    last_neigh_stdout = "<empty>"
    last_ping_rc: Optional[int] = None

    while time.monotonic() < deadline:
        attempt += 1
        initramfs_fatal = _detect_initramfs_fatal(serial_log_path=serial_log_path)
        if initramfs_fatal:
            raise CHExecuteError(
                f"Guest initramfs boot error before network ready: {initramfs_fatal}"
            )

        ping_result = _run(["ping", "-4", "-c", "1", "-W", "1", vm_ip], check=False)
        neigh_result = _run(["ip", "-4", "neigh", "show", vm_ip], check=False)

        last_ping_rc = ping_result.returncode
        last_ping_stdout = (ping_result.stdout or "").strip() or "<empty>"
        last_ping_stderr = (ping_result.stderr or "").strip() or "<empty>"
        last_neigh_stdout = (neigh_result.stdout or "").strip() or "<empty>"

        ready = ping_result.returncode == 0 or _neighbor_is_usable(last_neigh_stdout)
        log.LOGGER(
            f"[CH][{vmachine_id}] guest network readiness attempt={attempt}, "
            f"ping_rc={ping_result.returncode}, neigh={last_neigh_stdout}"
        )
        if ready:
            log.LOGGER(
                f"[CH][{vmachine_id}] guest network readiness passed after {attempt} attempt(s)"
            )
            return

        time.sleep(0.5)

    initramfs_fatal = _detect_initramfs_fatal(serial_log_path=serial_log_path)
    if initramfs_fatal:
        raise CHExecuteError(
            f"Guest initramfs boot error before network ready: {initramfs_fatal}"
        )

    raise CHExecuteError(
        f"Guest network did not become ready within {timeout_s:.1f}s for {vm_ip}. "
        f"last_ping_rc={last_ping_rc}, last_ping_stdout={last_ping_stdout}, "
        f"last_ping_stderr={last_ping_stderr}, last_neigh={last_neigh_stdout}. "
        "Check guest serial log and verify custom initramfs/rootfs entrypoint."
    )


def _resolve_guest_config_targets(service: celaut.Service) -> List[str]:
    # CH runtime expects the serialized configuration at the filesystem root.
    # We keep this deterministic regardless of service.container.config.path.
    _ = service
    return ["/__config__"]


def _kernel_cmdline(vm_ip: str, netmask: str) -> str:
    guest_dev = str(GUEST_NET_DEVICE).strip() if GUEST_NET_DEVICE is not None else ""
    if not guest_dev or guest_dev.lower() in {"auto", "none"}:
        # Leave the kernel interface field empty so it selects the first usable NIC.
        ip_param = f"ip={vm_ip}::{NETWORK_GATEWAY_IP}:{netmask}:::off"
    else:
        ip_param = f"ip={vm_ip}::{NETWORK_GATEWAY_IP}:{netmask}::{guest_dev}:off"

    cmdline_parts = ["root=/dev/vda", "rw", ip_param]
    extra = str(KERNEL_CMDLINE_EXTRA).strip() if KERNEL_CMDLINE_EXTRA is not None else ""
    if extra:
        cmdline_parts.append(extra)
    return " ".join(cmdline_parts)


def _tail_file(path: Path, max_lines: int = 40) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return "<missing>"
    except Exception as e:
        return f"<unreadable: {e}>"

    if not lines:
        return "<empty>"
    return "".join(lines[-max_lines:]).strip()


def execute(
    assigment_ports: Optional[Dict[int, int]],
    by_local: bool,
    service_id: str,
    service: celaut.Service,
    config: Optional[celaut.Configuration],
    initial_system_resources: celaut.Sysresources,
    father_id: str,
) -> Tuple[str, str]:
    vmachine_id = str(uuid4())
    runtime_dir = _runtime_vm_dir(vmachine_id)
    cleanup_rules: List[List[str]] = []
    tap_name: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    rootfs_path = runtime_dir / "rootfs.ext4"
    api_socket_path = runtime_dir / "cloud-hypervisor.sock"
    config_host_path = runtime_dir / "__config__"
    entrypoint_host_path = runtime_dir / ".__nodo_entrypoint"
    stdout_path = runtime_dir / "cloud-hypervisor.stdout.log"
    stderr_path = runtime_dir / "cloud-hypervisor.stderr.log"
    serial_log_path: Optional[Path] = None
    resolved_entrypoint: Optional[str] = None

    try:
        log.LOGGER(
            f"[CH][{vmachine_id}] execute start: service_id={service_id}, father_id={father_id}, "
            f"by_local={by_local}, assignment_ports={assigment_ports}, cache={CACHE}, "
            f"bridge={NETWORK_BRIDGE_NAME}, subnet={NETWORK_SUBNET}, gateway={NETWORK_GATEWAY_IP}"
        )

        log.LOGGER(f"[CH][{vmachine_id}] running network preflight")
        network = _network_preflight()
        log.LOGGER(f"[CH][{vmachine_id}] network preflight ok: {network.with_prefixlen}")

        ch_binary = _resolve_ch_binary()
        log.LOGGER(f"[CH][{vmachine_id}] cloud-hypervisor binary resolved: {ch_binary}")

        arch = _resolve_service_arch(service_id=service_id, service=service)
        bundle = _load_bundle(service_id=service_id, arch=arch)
        log.LOGGER(
            f"[CH][{vmachine_id}] bundle loaded: arch={bundle['arch']}, "
            f"rootfs={bundle['rootfs_path']}, kernel={bundle['kernel_path']}, "
            f"initramfs={bundle['initramfs_path']}"
        )
        _validate_custom_initramfs(bundle["initramfs_path"])
        log.LOGGER(f"[CH][{vmachine_id}] initramfs validation passed for {bundle['initramfs_path']}")

        resolved_entrypoint = _validate_entrypoint_strict(service=service)
        log.LOGGER(f"[CH][{vmachine_id}] validated strict entrypoint: {resolved_entrypoint}")

        vm_ip, mac = _deterministic_ip_and_mac(vmachine_id)
        log.LOGGER(f"[CH][{vmachine_id}] deterministic networking: ip={vm_ip}, mac={mac}")

        runtime_dir.mkdir(parents=True, exist_ok=True)
        log.LOGGER(f"[CH][{vmachine_id}] runtime dir prepared: {runtime_dir}")

        shutil.copy2(bundle["rootfs_path"], rootfs_path)
        log.LOGGER(f"[CH][{vmachine_id}] rootfs copied to runtime image: {rootfs_path}")

        network_resolution = _build_network_resolution(service=service, father_id=father_id)
        log.LOGGER(f"[CH][{vmachine_id}] network resolution entries: {len(network_resolution)}")
        cfg = _build_configuration_file(
            config=config,
            resources=initial_system_resources,
            network_resolution=network_resolution,
        )

        with open(config_host_path, "wb") as f:
            f.write(cfg.SerializeToString())
        log.LOGGER(
            f"[CH][{vmachine_id}] config serialized: {config_host_path} "
            f"({config_host_path.stat().st_size} bytes)"
        )

        config_targets = _resolve_guest_config_targets(service=service)
        log.LOGGER(
            f"[CH][{vmachine_id}] guest config targets={config_targets} "
            f"(service.container.config.path={list(service.container.config.path)})"
        )
        for target_path in config_targets:
            log.LOGGER(f"[CH][{vmachine_id}] injecting config into guest target: {target_path}")
            _run_debugfs_write(
                image_path=rootfs_path,
                host_file=config_host_path,
                guest_target=target_path,
            )
        log.LOGGER(f"[CH][{vmachine_id}] guest config injection completed for {len(config_targets)} target(s)")

        with open(entrypoint_host_path, "w", encoding="utf-8") as f:
            f.write(f"{resolved_entrypoint}\n")
        log.LOGGER(f"[CH][{vmachine_id}] entrypoint metadata serialized: {entrypoint_host_path}")
        _run_debugfs_write(
            image_path=rootfs_path,
            host_file=entrypoint_host_path,
            guest_target="/.__nodo_entrypoint",
        )
        log.LOGGER(f"[CH][{vmachine_id}] guest entrypoint injection completed: /.__nodo_entrypoint")

        tap_name = _create_tap(vmachine_id)
        log.LOGGER(f"[CH][{vmachine_id}] TAP created and attached: {tap_name}")
        _log_host_network_probe(vmachine_id=vmachine_id, vm_ip=vm_ip, tap_name=tap_name)

        vcpus, mem_mib = _resolve_initial_resources(initial_system_resources)
        netmask = str(network.netmask)
        kernel_cmdline = _kernel_cmdline(vm_ip=vm_ip, netmask=netmask)
        log.LOGGER(
            f"[CH][{vmachine_id}] VM resources: vcpus={vcpus}, mem_mib={mem_mib}, "
            f"guest_net_device={GUEST_NET_DEVICE}, kernel_cmdline={kernel_cmdline}"
        )

        stream_args, serial_log_path = _resolve_ch_stream_args(runtime_dir=runtime_dir)
        if serial_log_path:
            log.LOGGER(f"[CH][{vmachine_id}] guest serial log path: {serial_log_path}")
        if stream_args:
            log.LOGGER(f"[CH][{vmachine_id}] cloud-hypervisor stream args: {' '.join(stream_args)}")

        # Explicitly declare raw image type to avoid CH autodetection safeguards
        # that can mark sector-0 writes as read-only and break ext4 rw mounts.
        disk_arg = f"path={rootfs_path},image_type=raw"
        start_command = [
            ch_binary,
            "--api-socket",
            str(api_socket_path),
            "--kernel",
            bundle["kernel_path"],
            "--initramfs",
            bundle["initramfs_path"],
            "--disk",
            disk_arg,
            "--cpus",
            f"boot={vcpus}",
            "--memory",
            f"size={mem_mib}M",
            "--net",
            f"tap={tap_name},mac={mac}",
            "--cmdline",
            kernel_cmdline,
        ]
        start_command.extend(stream_args)
        log.LOGGER(f"[CH][{vmachine_id}] launching cloud-hypervisor: {' '.join(start_command)}")

        with open(stdout_path, "w", encoding="utf-8") as stdout_file, open(
            stderr_path, "w", encoding="utf-8"
        ) as stderr_file:
            process = subprocess.Popen(start_command, stdout=stdout_file, stderr=stderr_file)
        log.LOGGER(
            f"[CH][{vmachine_id}] process started: pid={process.pid}, api_socket={api_socket_path}, "
            f"stdout={stdout_path}, stderr={stderr_path}"
        )

        time.sleep(1.0)
        if process.poll() is not None:
            raise CHExecuteError(
                f"cloud-hypervisor process exited early with code {process.returncode}. "
                f"See {stderr_path}. stderr tail: {_tail_file(stderr_path)}"
            )
        log.LOGGER(f"[CH][{vmachine_id}] process health check passed after 1s")

        network_timeout_s = _guest_network_ready_timeout_seconds()
        log.LOGGER(
            f"[CH][{vmachine_id}] waiting guest network readiness: vm_ip={vm_ip}, timeout={network_timeout_s}s"
        )
        _wait_guest_network_ready(
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            timeout_s=network_timeout_s,
            serial_log_path=serial_log_path,
        )
        _log_host_network_probe(vmachine_id=vmachine_id, vm_ip=vm_ip, tap_name=tap_name)

        dnat_rules_state: List[Dict[str, object]] = []
        if not by_local and assigment_ports:
            for internal_port, external_port in assigment_ports.items():
                for protocol in ("tcp", "udp"):
                    removal_commands = _add_dnat_rule(
                        protocol=protocol,
                        external_port=external_port,
                        vm_ip=vm_ip,
                        internal_port=internal_port,
                    )
                    cleanup_rules.extend(removal_commands)
                    dnat_rules_state.append(
                        {
                            "protocol": protocol,
                            "external_port": external_port,
                            "internal_port": internal_port,
                            "destination_ip": vm_ip,
                        }
                    )
                    log.LOGGER(
                        f"[CH][{vmachine_id}] DNAT rule added: {protocol} "
                        f"host:{external_port} -> guest:{vm_ip}:{internal_port}"
                    )

        save_runtime_state(
            vmachine_id,
            {
                "vmachine_id": vmachine_id,
                "service_id": service_id,
                "arch": bundle["arch"],
                "pid": process.pid,
                "api_socket": str(api_socket_path),
                "tap": tap_name,
                "ip": vm_ip,
                "mac": mac,
                "rootfs_path": str(rootfs_path),
                "entrypoint": resolved_entrypoint,
                "dnat_rules": dnat_rules_state,
                "bridge": NETWORK_BRIDGE_NAME,
                "serial_log": str(serial_log_path) if serial_log_path else "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if CACHE:
            state_path = Path(CACHE) / "cloud_hypervisor" / "runtime" / f"{vmachine_id}.json"
            log.LOGGER(f"[CH][{vmachine_id}] runtime state persisted: {state_path}")

        # Keep gateway egress behavior aligned with Docker semantics for now.
        log.LOGGER(
            f"Cloud Hypervisor VM started: {vmachine_id} ({vm_ip}), runtime_dir={runtime_dir}"
        )
        return vmachine_id, vm_ip

    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] execute failed: {type(e).__name__}: {e}")
        log.LOGGER(f"[CH][{vmachine_id}] traceback:\n{traceback.format_exc()}")
        if process:
            log.LOGGER(
                f"[CH][{vmachine_id}] process state at failure: pid={process.pid}, poll={process.poll()}"
            )
        log.LOGGER(f"[CH][{vmachine_id}] stdout tail ({stdout_path}): {_tail_file(stdout_path)}")
        log.LOGGER(f"[CH][{vmachine_id}] stderr tail ({stderr_path}): {_tail_file(stderr_path)}")
        if serial_log_path:
            log.LOGGER(f"[CH][{vmachine_id}] serial tail ({serial_log_path}): {_tail_file(serial_log_path)}")

        if cleanup_rules:
            log.LOGGER(f"[CH][{vmachine_id}] removing {len(cleanup_rules)} cleanup firewall rules")
        _remove_rules(cleanup_rules)

        if process and process.poll() is None:
            log.LOGGER(f"[CH][{vmachine_id}] terminating cloud-hypervisor process pid={process.pid}")
            process.terminate()
            time.sleep(0.5)
            if process.poll() is None:
                log.LOGGER(f"[CH][{vmachine_id}] killing cloud-hypervisor process pid={process.pid}")
                process.kill()

        if tap_name:
            log.LOGGER(f"[CH][{vmachine_id}] deleting TAP interface: {tap_name}")
            _delete_tap(tap_name)

        log.LOGGER(f"[CH][{vmachine_id}] deleting runtime state entry")
        delete_runtime_state(vmachine_id)

        if runtime_dir.exists():
            log.LOGGER(f"[CH][{vmachine_id}] removing runtime directory: {runtime_dir}")
            shutil.rmtree(runtime_dir, ignore_errors=True)

        if isinstance(e, CHExecuteError):
            raise
        raise CHExecuteError(str(e)) from e
