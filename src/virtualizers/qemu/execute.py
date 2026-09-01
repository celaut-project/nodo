"""QEMU (TCG) execution backend: run a service whose architecture differs from
the host, under software emulation.

This is the emulated sibling of :mod:`src.virtualizers.ch.execute`. Cloud
Hypervisor boots a service as a microVM under KVM, which can only run a guest of
the host's own architecture. When a request targets a foreign arch (the driving
case: an ``arm64`` service on an ``x86_64`` node), there is no KVM path -- so this
backend boots the same rootfs/kernel/initramfs under ``qemu-system-<arch>`` with
``-accel tcg``.

**Deliberate reuse of the CH backend.** Everything that is *not* the hypervisor
invocation is shared with CH by importing its helpers directly, for two reasons:

* Correctness, not just convenience. Host networking is a single shared resource
  -- one bridge, one subnet, one IP/MAC allocator, one runtime-state store that
  :func:`_used_ips` and the janitor read. A QEMU guest MUST allocate IPs and TAPs
  through the very same code CH uses, or the two backends would hand out
  colliding addresses. Runtime state is therefore written to the same store
  (tagged ``virtualizer: "qemu"``), so IP accounting, the janitor and firewall IP
  resolution all see QEMU guests too.
* The rootfs build, config/DNS/entrypoint injection, TAP/firewall setup and
  guest-network readiness probing are byte-for-byte identical to CH; duplicating
  them would be 400 lines of drift waiting to happen.

Only the launch itself differs, and that lives in the pure builders
(:func:`build_kernel_cmdline`, :func:`build_qemu_command`) plus :func:`execute`.

A future cleanup could promote the shared helpers into a neutral module; kept as
an explicit import here to leave the proven CH path untouched.
"""
import math
import os
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.virtualizers.ch import execute as ch_exec
from src.virtualizers.ch import limits
from src.virtualizers.ch.cgroups import apply_cpu_limit, apply_memory_limit, ensure_vm_cgroup
from src.virtualizers.ch.runtime_state import save_runtime_state, save_booting_state, delete_runtime_state
from src.virtualizers.ch.virtiofs import (
    attach_virtiofs_backends,
    build_guest_mount_plan,
    child_guest_mounts,
    parent_export_mounts,
    shared_fs_base_dir,
    GUEST_MOUNT_PLAN_PATH,
)
from src.virtualizers.qemu.config import (
    QEMU_CONSOLE_BY_ARCH,
    QEMU_MACHINE_BY_ARCH,
    qemu_initramfs_path,
    qemu_kernel_path,
    qemu_system_binary,
)
from src.virtualizers.qemu.process import qemu_process_name
from src.utils.firewall import policy as fw_policy
from src.virtualizers.firewall import resolve_slot_transport_protocols

env_manager = ConfigManager()
sc = SQLConnection()

CACHE = env_manager.get("CACHE")
CH_API_SOCKET_DIR = env_manager.get("virtualizers.ch.API_SOCKET_DIR", "/tmp/nodo-ch")
VIRTIOFSD_BINARY = env_manager.get("virtualizers.ch.VIRTIOFSD_BINARY", "virtiofsd")
NETWORK_BRIDGE_NAME = ch_exec.NETWORK_BRIDGE_NAME
NETWORK_GATEWAY_IP = ch_exec.NETWORK_GATEWAY_IP
QEMU_CPU_MODEL = env_manager.get("virtualizers.qemu.CPU_MODEL", "max")
# TCG is slow to reach console; give the guest more time than the KVM default.
QEMU_NETWORK_READY_TIMEOUT_S = env_manager.get(
    "virtualizers.qemu.GUEST_NETWORK_READY_TIMEOUT_S",
    120,
)


class QEMUExecuteError(RuntimeError):
    pass


def _runtime_vm_dir(vmachine_id: str) -> Path:
    if not CACHE:
        raise QEMUExecuteError("CACHE path is not configured.")
    # Runtime dirs live under the shared cloud_hypervisor runtime tree so the
    # janitor and IP accounting see QEMU guests alongside CH ones.
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _qmp_socket_path(vmachine_id: str) -> Path:
    # Kept in the short CH API socket dir, not runtime_dir, to stay under the
    # AF_UNIX SUN_LEN limit (runtime_dir is nested under CACHE and keyed by the
    # full 64-hex vmachine_id, which alone can exceed the 108-byte limit).
    return Path(CH_API_SOCKET_DIR) / f"qmp-{vmachine_id[:16]}.sock"


# --------------------------------------------------------------------------- #
# Pure builders — unit-tested directly, no side effects.
# --------------------------------------------------------------------------- #

def build_kernel_cmdline(arch: str, vm_ip: str, netmask: str) -> str:
    """Guest kernel cmdline for an emulated boot.

    Reuses CH's ``ip=`` autoconfig token (same bridge/gateway networking) and
    ``root=/dev/vda`` (virtio-blk), but pins ``console=`` to the arch's serial
    device so init output reaches the captured serial log -- ``ttyAMA0`` for the
    arm64 ``virt`` PL011, ``ttyS0`` for the x86 16550.
    """
    guest_dev = str(ch_exec.GUEST_NET_DEVICE).strip() if ch_exec.GUEST_NET_DEVICE is not None else ""
    if not guest_dev or guest_dev.lower() in {"auto", "none"}:
        ip_param = f"ip={vm_ip}::{NETWORK_GATEWAY_IP}:{netmask}:::off"
    else:
        ip_param = f"ip={vm_ip}::{NETWORK_GATEWAY_IP}:{netmask}::{guest_dev}:off"

    console = QEMU_CONSOLE_BY_ARCH.get(arch, "ttyS0")
    return " ".join(["root=/dev/vda", "rw", ip_param, f"console={console}"])


def build_virtiofs_args(
    mounts_state: List[dict],
    mem_mib: int,
) -> List[str]:
    """QEMU device args for the shared filesystems in ``mounts_state``.

    vhost-user-fs requires the guest memory to be a shared backend, so when any
    share is present the VM's RAM is provided via ``memory-backend-memfd`` +
    ``-numa node`` and each virtiofsd socket is attached as a
    ``vhost-user-fs-pci`` device. With no shares this returns ``[]`` and the
    caller uses a plain ``-m <mib>M`` (the common path). Only exercised for
    services that declare shared/guest directories.
    """
    if not mounts_state:
        return []

    args: List[str] = [
        "-object",
        f"memory-backend-memfd,id=mem,size={mem_mib}M,share=on",
        "-numa",
        "node,memdev=mem",
    ]
    for index, backend in enumerate(mounts_state):
        socket_path = backend["socket"]
        tag = backend["tag"]
        args.extend(
            [
                "-chardev",
                f"socket,id=vfs{index},path={socket_path}",
                "-device",
                f"vhost-user-fs-pci,queue-size=1024,chardev=vfs{index},tag={tag}",
            ]
        )
    return args


# Names the stats polling interval may go by, most likely first. Every QEMU
# checked spells it `guest-stats-polling-interval` (6.2 on Ubuntu 22.04 through
# 8.2); `stats-polling-interval` is carried only as a fallback for a build that
# does not, and is never assumed. The list exists because naming a property the
# binary does not have is fatal at launch rather than ignored -- an emulator that
# exits with "Property ... not found" takes the whole instance with it -- so the
# name is picked from what the binary advertises, never guessed.
_BALLOON_STATS_INTERVAL_PROPERTIES = (
    "guest-stats-polling-interval",
    "stats-polling-interval",
)

# Polling seconds. Only needs to be frequent enough that a resize arriving after
# boot sees a fresh reading.
_BALLOON_STATS_INTERVAL_SECONDS = 2


@lru_cache(maxsize=8)
def _balloon_properties(qemu_binary: str) -> frozenset:
    """Property names this QEMU's ``virtio-balloon-pci`` accepts.

    Asked once per binary and cached: the answer cannot change under a running
    node, and every guest launch would otherwise pay for the probe.
    """
    try:
        proc = subprocess.run(
            [qemu_binary, "-machine", "virt", "-device", "virtio-balloon-pci,help"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return frozenset()

    names = set()
    for line in (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return frozenset(names)


def _balloon_device_arg(qemu_binary: str) -> str:
    """``-device`` argument for the balloon, using only properties this QEMU has.

    ``id`` is fixed so hotplug can address the device by QOM path. The optional
    extras are what make a shrink safe rather than fatal:

    * a stats polling interval is what lets the guest report its free memory, so
      a resize can be clamped to what the guest can actually spare instead of
      OOM-panicking it;
    * ``free-page-reporting`` lets the guest return pages it frees on its own;
    * ``deflate-on-oom`` makes the guest give itself memory back rather than die
      if it is ever squeezed too far anyway -- the last line of defence.

    Each is included only when this QEMU advertises it, because an unknown
    property is a launch failure, not a warning.
    """
    available = _balloon_properties(qemu_binary)
    parts = ["virtio-balloon-pci", "id=nodo-balloon"]

    for prop in _BALLOON_STATS_INTERVAL_PROPERTIES:
        if prop in available:
            parts.append(f"{prop}={_BALLOON_STATS_INTERVAL_SECONDS}")
            break

    for prop in ("free-page-reporting", "deflate-on-oom"):
        if prop in available:
            parts.append(f"{prop}=on")

    return ",".join(parts)


def build_qemu_command(
    *,
    qemu_binary: str,
    arch: str,
    kernel_path: str,
    initramfs_path: str,
    rootfs_path: Path,
    vcpus: int,
    mem_mib: int,
    tap_name: str,
    mac: str,
    cmdline: str,
    serial_log_path: Path,
    virtiofs_args: Optional[List[str]] = None,
    has_shared_mem: bool = False,
    qmp_socket_path: Optional[str] = None,
) -> List[str]:
    """Full ``qemu-system-<arch>`` argv for one emulated guest.

    Emulation (``-accel tcg``) is explicit: this path exists precisely because
    KVM cannot run a foreign arch. The disk is virtio-blk (``/dev/vda``), the NIC
    a virtio-net-pci on the pre-created host TAP (``script=no``: nodo owns bridge
    attachment), and serial is redirected to a file for boot diagnostics.
    """
    machine = QEMU_MACHINE_BY_ARCH.get(arch, "q35")
    command: List[str] = [
        qemu_binary,
        "-machine",
        machine,
        "-accel",
        "tcg",
        "-cpu",
        QEMU_CPU_MODEL,
        "-smp",
        str(vcpus),
    ]

    # Memory: a shared memfd backend is required as soon as virtiofs is attached;
    # otherwise a plain size flag.
    if has_shared_mem:
        # `-object memory-backend-memfd` (added by build_virtiofs_args) provides
        # the RAM; -m still declares the size to the machine.
        command.extend(["-m", str(mem_mib)])
    else:
        command.extend(["-m", f"{mem_mib}M"])

    command.extend(
        [
            "-kernel",
            str(kernel_path),
            "-initrd",
            str(initramfs_path),
            "-append",
            cmdline,
            "-drive",
            f"if=virtio,file={rootfs_path},format=raw",
            "-netdev",
            f"tap,id=net0,ifname={tap_name},script=no,downscript=no",
            "-device",
            f"virtio-net-pci,netdev=net0,mac={mac}",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            f"file:{serial_log_path}",
            "-no-reboot",
        ]
    )

    # virtio-balloon + QMP control socket: the live memory-resize path. Memory
    # hotplug drives the balloon over this socket (src/virtualizers/qemu/hotplug.py)
    # so the guest actually returns pages, instead of the cgroup squeezing the
    # qemu process into swap/OOM. Harmless when hotplug is never called.
    command.extend(["-device", _balloon_device_arg(qemu_binary)])
    if qmp_socket_path:
        command.extend(["-qmp", f"unix:{qmp_socket_path},server=on,wait=off"])

    if virtiofs_args:
        command.extend(virtiofs_args)

    return command


def _build_process_args(start_command: List[str], vmachine_id: str) -> List[str]:
    visible_name = qemu_process_name(vmachine_id)
    return [visible_name, *start_command[1:]] if start_command else [visible_name]


# --------------------------------------------------------------------------- #
# Launch
# --------------------------------------------------------------------------- #

def execute(
    assigment_ports: Optional[Dict[int, int]],
    by_local: bool,
    service_id: str,
    service: celaut.Service,
    config: Optional[celaut.Configuration],
    initial_system_resources: celaut.Sysresources,
    father_id: str,
    register_instance: Optional[Callable[[str, str, celaut.Sysresources], None]] = None,
) -> Tuple[str, str, celaut.Sysresources]:
    """Emulated counterpart of :func:`src.virtualizers.ch.execute.execute`.

    Same contract: build the guest from its bundle, wire host networking and
    firewall, boot it, wait for the guest to come up, and return
    ``(vmachine_id, vm_ip, resolved_resources)``. Only the hypervisor process
    differs -- including ``register_instance``, which is called the instant the
    emulator process exists so the node can identify the guest by its address
    before the guest calls in.
    """
    vmachine_id = ch_exec._generate_vmachine_id()
    runtime_dir = _runtime_vm_dir(vmachine_id)
    cleanup_rules: List[List[str]] = []
    tap_name: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    rootfs_path = runtime_dir / "rootfs.ext4"
    config_host_path = runtime_dir / "__config__"
    entrypoint_host_path = runtime_dir / ".__nodo_entrypoint"
    stdout_path = runtime_dir / "qemu.stdout.log"
    stderr_path = runtime_dir / "qemu.stderr.log"
    serial_log_path = runtime_dir / "qemu.serial.log"
    qmp_socket_path = _qmp_socket_path(vmachine_id)
    resolved_entrypoint: Optional[str] = None

    try:
        log.LOGGER(f"[QEMU][{vmachine_id}] event=start")
        log.LOGGER(
            f"[QEMU][{vmachine_id}] execute start: service_id={service_id}, father_id={father_id}, "
            f"by_local={by_local}, assignment_ports={assigment_ports}, cache={CACHE}"
        )

        network = ch_exec._network_preflight()
        log.LOGGER(f"[QEMU][{vmachine_id}] network preflight ok: {network.with_prefixlen}")

        arch = ch_exec._resolve_service_arch(service_id=service_id, service=service)
        qemu_binary = qemu_system_binary(arch)
        if not qemu_binary:
            raise UnsupportedArchitectureException(arch=arch)
        log.LOGGER(f"[QEMU][{vmachine_id}] emulator resolved: {qemu_binary} (arch={arch})")

        # Kernel/initramfs: QEMU override first, else the CH bundle's assets. The
        # bundle still validates that the CH-side assets exist for the arch.
        bundle = ch_exec._load_bundle(service_id=service_id, arch=arch)
        kernel_path = qemu_kernel_path(arch) or bundle["kernel_path"]
        initramfs_path = qemu_initramfs_path(arch) or bundle["initramfs_path"]
        if not os.path.isfile(kernel_path):
            raise QEMUExecuteError(f"QEMU guest kernel not found for {arch}: {kernel_path}")
        if not os.path.isfile(initramfs_path):
            raise QEMUExecuteError(f"QEMU guest initramfs not found for {arch}: {initramfs_path}")
        log.LOGGER(
            f"[QEMU][{vmachine_id}] bundle loaded: arch={arch}, rootfs={bundle['rootfs_path']}, "
            f"kernel={kernel_path}, initramfs={initramfs_path}"
        )
        ch_exec._validate_custom_initramfs(initramfs_path)

        resolved_entrypoint = ch_exec._validate_entrypoint_strict(service=service)
        log.LOGGER(f"[QEMU][{vmachine_id}] validated strict entrypoint: {resolved_entrypoint}")

        vm_ip, mac = ch_exec._deterministic_ip_and_mac(vmachine_id)
        log.LOGGER(f"[QEMU][{vmachine_id}] deterministic networking: ip={vm_ip}, mac={mac}")

        runtime_dir.mkdir(parents=True, exist_ok=True)
        qmp_socket_path.parent.mkdir(parents=True, exist_ok=True)
        log.LOGGER(f"[QEMU][{vmachine_id}] QMP socket dir prepared: {qmp_socket_path.parent}")
        shutil.copy2(bundle["rootfs_path"], rootfs_path)
        log.LOGGER(f"[QEMU][{vmachine_id}] rootfs copied to runtime image: {rootfs_path}")

        network_resolution = ch_exec._build_network_resolution(
            service=service, father_id=father_id, config=config
        )
        cfg = ch_exec._build_configuration_file(
            config=config,
            resources=initial_system_resources,
            network_resolution=network_resolution,
        )
        with open(config_host_path, "wb") as f:
            f.write(cfg.SerializeToString())

        for target_path in ch_exec._resolve_guest_config_targets(service=service):
            ch_exec._run_debugfs_write(
                image_path=rootfs_path,
                host_file=config_host_path,
                guest_target=target_path,
            )

        with open(entrypoint_host_path, "w", encoding="utf-8") as f:
            f.write(f"{resolved_entrypoint}\n")
        ch_exec._run_debugfs_write(
            image_path=rootfs_path,
            host_file=entrypoint_host_path,
            guest_target="/.__nodo_entrypoint",
        )
        # No /etc/hosts nor /etc/resolv.conf: name resolution is not the node's to
        # install in someone else's filesystem. See the note above
        # `_configure_guest_firewall_policy` in src/virtualizers/ch/execute.py.
        log.LOGGER(f"[QEMU][{vmachine_id}] guest metadata injected (config/entrypoint)")

        # Shared filesystems (parent -> child inheritance). Identical semantics to
        # CH; only the guest device wiring (vhost-user-fs vs CH --fs) differs.
        shared_fs_dir = str(shared_fs_base_dir(CACHE)) if CACHE else None
        virtiofs_mounts: List[dict] = []
        virtiofs_args: List[str] = []
        exported_share_ids: List[str] = []
        share_mounts: List = []
        if shared_fs_dir:
            export_mounts = parent_export_mounts(service, vmachine_id, shared_fs_dir)
            guest_mounts = child_guest_mounts(service, father_id, shared_fs_dir)
            share_mounts = export_mounts + guest_mounts
            if share_mounts:
                log.LOGGER(
                    f"[QEMU][{vmachine_id}] shared filesystems: {len(export_mounts)} exported, "
                    f"{len(guest_mounts)} inherited from father={father_id}"
                )
                _, virtiofs_mounts, _ = attach_virtiofs_backends(
                    share_mounts,
                    base_dir=shared_fs_dir,
                    socket_dir=CH_API_SOCKET_DIR,
                    virtiofsd_binary=VIRTIOFSD_BINARY,
                    logger_fn=log.LOGGER,
                )
                exported_share_ids = [m.share_id_hex for m in export_mounts]
                mount_plan_host_path = runtime_dir / ".__nodo_virtiofs"
                with open(mount_plan_host_path, "w", encoding="utf-8") as f:
                    f.write(build_guest_mount_plan(share_mounts))
                ch_exec._run_debugfs_write(
                    image_path=rootfs_path,
                    host_file=mount_plan_host_path,
                    guest_target=GUEST_MOUNT_PLAN_PATH,
                )

        tap_name = ch_exec._create_tap(vmachine_id)
        log.LOGGER(f"[QEMU][{vmachine_id}] TAP created and attached: {tap_name}")

        # Committed before the guest exists, not after it starts pinging. See
        # src/virtualizers/ch/execute.py for why: none of this needs the guest to
        # be alive, and the tap above is already forwarding-capable.
        ch_exec._configure_guest_firewall_policy(
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            network_resolution=network_resolution,
        )

        vcpus, mem_b, cpu_quota, cpu_period = limits.resolve_initial_resources(initial_system_resources)
        disk_b = ch_exec._runtime_disk_bytes(vmachine_id=vmachine_id, rootfs_path=rootfs_path)
        resolved_resources = celaut.Sysresources(
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            mem_limit=mem_b,
            disk_space=disk_b,
        )
        mem_mib = math.ceil(mem_b / (1024 * 1024))
        netmask = str(network.netmask)
        cmdline = build_kernel_cmdline(arch=arch, vm_ip=vm_ip, netmask=netmask)

        has_shared_mem = bool(share_mounts)
        virtiofs_args = build_virtiofs_args(virtiofs_mounts, mem_mib) if has_shared_mem else []

        start_command = build_qemu_command(
            qemu_binary=qemu_binary,
            arch=arch,
            kernel_path=kernel_path,
            initramfs_path=initramfs_path,
            rootfs_path=rootfs_path,
            vcpus=vcpus,
            mem_mib=mem_mib,
            tap_name=tap_name,
            mac=mac,
            cmdline=cmdline,
            serial_log_path=serial_log_path,
            virtiofs_args=virtiofs_args,
            has_shared_mem=has_shared_mem,
            qmp_socket_path=str(qmp_socket_path),
        )
        log.LOGGER(
            f"[QEMU][{vmachine_id}] VM resources: vcpus={vcpus}, mem_mib={mem_mib}, "
            f"cmdline={cmdline}"
        )
        log.LOGGER(f"[QEMU][{vmachine_id}] launching qemu: {' '.join(start_command)}")

        process_args = _build_process_args(start_command=start_command, vmachine_id=vmachine_id)
        with open(stdout_path, "w", encoding="utf-8") as stdout_file, open(
            stderr_path, "w", encoding="utf-8"
        ) as stderr_file:
            process = subprocess.Popen(
                process_args,
                executable=qemu_binary,
                stdout=stdout_file,
                stderr=stderr_file,
            )
        log.LOGGER(
            f"[QEMU][{vmachine_id}] process started: pid={process.pid}, visible_name={process_args[0]}"
        )

        # The guest is running from here; record it before it can call the node.
        # See src/virtualizers/ch/execute.py for why this is not left to the end.
        save_booting_state(
            vmachine_id,
            virtualizer="qemu",
            service_id=service_id,
            pid=process.pid,
            ip=vm_ip,
            mac=mac,
            tap=tap_name,
            bridge=NETWORK_BRIDGE_NAME,
            cleanup_rules=cleanup_rules,
            rule_comment_prefix=fw_policy.vm_comment_prefix(vmachine_id),
        )
        if register_instance:
            register_instance(vmachine_id, vm_ip, resolved_resources)
            log.LOGGER(f"[QEMU][{vmachine_id}] instance registered before the guest could call in")

        time.sleep(1.0)
        if process.poll() is not None:
            raise QEMUExecuteError(
                f"qemu process exited early with code {process.returncode}. "
                f"See {stderr_path}. stderr tail: {ch_exec._tail_file(stderr_path)}"
            )

        vm_cgroup: Path = ensure_vm_cgroup(vmachine_id=vmachine_id, pid=process.pid)
        apply_memory_limit(vm_cgroup=vm_cgroup, mem_limit=mem_b)
        apply_cpu_limit(vm_cgroup=vm_cgroup, cpu_quota=cpu_quota, cpu_period=cpu_period)

        network_timeout_s = float(QEMU_NETWORK_READY_TIMEOUT_S)
        log.LOGGER(
            f"[QEMU][{vmachine_id}] waiting guest network readiness: vm_ip={vm_ip}, timeout={network_timeout_s}s"
        )
        ch_exec._wait_guest_network_ready(
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            timeout_s=network_timeout_s,
            serial_log_path=serial_log_path,
        )
        log.LOGGER(f"[QEMU][{vmachine_id}] event=ready")

        dnat_rules_state: List[Dict[str, object]] = []
        if not by_local and assigment_ports:
            slot_by_port = {slot.port: slot for slot in service.api.slot}
            for internal_port, external_port in assigment_ports.items():
                slot = slot_by_port.get(internal_port)
                if not slot:
                    continue
                protocol = resolve_slot_transport_protocols(
                    slot, logger_fn=log.LOGGER, context=f"[QEMU][{vmachine_id}]"
                )
                if not protocol:
                    continue
                ch_exec._add_dnat_rule(
                    vmachine_id=vmachine_id,
                    protocol=protocol.value,
                    external_port=external_port,
                    vm_ip=vm_ip,
                    internal_port=internal_port,
                )
                dnat_rules_state.append(
                    {
                        "protocol": protocol.value,
                        "external_port": external_port,
                        "internal_port": internal_port,
                        "destination_ip": vm_ip,
                    }
                )
                log.LOGGER(
                    f"[QEMU][{vmachine_id}] DNAT rule added: {protocol.value} "
                    f"host:{external_port} -> guest:{vm_ip}:{internal_port}"
                )

        save_runtime_state(
            vmachine_id,
            {
                "vmachine_id": vmachine_id,
                "virtualizer": "qemu",
                "service_id": service_id,
                "arch": arch,
                "pid": process.pid,
                "tap": tap_name,
                "ip": vm_ip,
                "mac": mac,
                "rootfs_path": str(rootfs_path),
                "entrypoint": resolved_entrypoint,
                "dnat_rules": dnat_rules_state,
                "cleanup_rules": cleanup_rules,
                "rule_comment_prefix": fw_policy.vm_comment_prefix(vmachine_id),
                "cgroup_path": vm_cgroup.as_posix(),
                "qmp_socket": str(qmp_socket_path),
                "boot_mem_bytes": mem_b,
                "virtiofs": virtiofs_mounts,
                "exported_shares": exported_share_ids,
                "bridge": NETWORK_BRIDGE_NAME,
                "serial_log": str(serial_log_path),
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        log.LOGGER(
            f"QEMU VM started: {vmachine_id} ({vm_ip}), arch={arch}, runtime_dir={runtime_dir}"
        )
        return vmachine_id, vm_ip, resolved_resources

    except Exception as e:
        log.LOGGER(f"[QEMU][{vmachine_id}] execute failed: {type(e).__name__}: {e}")
        log.LOGGER(f"[QEMU][{vmachine_id}] traceback:\n{traceback.format_exc()}")
        log.LOGGER(f"[QEMU][{vmachine_id}] stderr tail ({stderr_path}): {ch_exec._tail_file(stderr_path)}")
        log.LOGGER(f"[QEMU][{vmachine_id}] serial tail ({serial_log_path}): {ch_exec._tail_file(serial_log_path)}")

        ch_exec._remove_rules(cleanup_rules)
        # Whatever was applied before the failure carries this VM's prefix.
        try:
            from src.virtualizers.firewall import remove_vm_rules

            remove_vm_rules(vmachine_id=vmachine_id)
        except Exception as e:
            log.LOGGER(f"[QEMU][{vmachine_id}] could not remove this VM's firewall rules: {e}")

        if process and process.poll() is None:
            process.terminate()
            time.sleep(0.5)
            if process.poll() is None:
                process.kill()

        if tap_name:
            ch_exec._delete_tap(tap_name)

        delete_runtime_state(vmachine_id)

        if runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)

        if isinstance(e, (QEMUExecuteError, UnsupportedArchitectureException)):
            raise
        raise QEMUExecuteError(str(e)) from e
