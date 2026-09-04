"""Cloud Hypervisor execution backend: boot a native-arch service under KVM.

The default path, and the reason the node's performance baseline is what it is: a
guest of the host's own architecture runs on hardware virtualization with no
emulation anywhere in it.

What is here is only what is CH's: resolving its binary, its ``--api-socket``
control channel, its ``--serial``/``--console`` stream arguments, the cmdline it
hands the guest, and the process invocation. Everything else a locally booted
Linux microVM needs -- the bundle, the rootfs injections, the tap and the
firewall, the cgroup, the runtime state, liveness, teardown, the janitor -- lives
in ``src.virtualizers.microvm`` and is shared with QEMU, which boots the same
bundles under emulation. See ``docs/BACKENDS.md`` for why that split is drawn
where it is.
"""
import math
import os
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.firewall import policy as fw_policy
from src.virtualizers.firewall import resolve_slot_transport_protocols, remove_vm_rules as vm_remove_vm_rules
from src.virtualizers.microvm import bundle as microvm_bundle
from src.virtualizers.microvm import guest as microvm_guest
from src.virtualizers.microvm import limits, network, paths, rootfs, serial
from src.virtualizers.microvm.cgroups import apply_cpu_limit, apply_memory_limit, ensure_vm_cgroup
from src.virtualizers.microvm.errors import MicroVMError
from src.virtualizers.microvm.members import CH
from src.virtualizers.microvm.process import generate_vmachine_id
from src.virtualizers.microvm.runtime_state import (
    delete_runtime_state,
    save_booting_state,
    save_runtime_state,
)
from src.virtualizers.microvm.virtiofs import (
    attach_virtiofs_backends,
    build_guest_mount_plan,
    child_guest_mounts,
    parent_export_mounts,
    shared_fs_base_dir,
    GUEST_MOUNT_PLAN_PATH,
)

env_manager = ConfigManager()

CH_BINARY_PATH = env_manager.get("virtualizers.ch.BINARY_PATH")
# No console here: the cmdline builder derives it from the architecture.
# Defaulting it to console=ttyS0 is what made arm64 guests panic before /init
# printed anything.
KERNEL_CMDLINE_EXTRA = env_manager.get("virtualizers.ch.KERNEL_CMDLINE_EXTRA", "")
CH_SERIAL_MODE = env_manager.get("virtualizers.ch.SERIAL_MODE", "file")
CH_CONSOLE_MODE = env_manager.get("virtualizers.ch.CONSOLE_MODE", "off")
VIRTIOFSD_BINARY = env_manager.get("virtualizers.ch.VIRTIOFSD_BINARY", "virtiofsd")
GUEST_NETWORK_READY_TIMEOUT_S = env_manager.get(
    "virtualizers.ch.GUEST_NETWORK_READY_TIMEOUT_S",
    8,
)
CONSERVE_RUNTIME_DIR_ON_FAILURE = env_manager.get("virtualizers.ch.CONSERVE_RUNTIME_DIR_ON_FAILURE", False)

def _api_socket_path(vmachine_id: str) -> Path:
    return CH.control_socket(vmachine_id)


def _resolve_ch_binary() -> str:
    if CH_BINARY_PATH:
        if os.path.isfile(CH_BINARY_PATH) and os.access(CH_BINARY_PATH, os.X_OK):
            return CH_BINARY_PATH
        raise MicroVMError(
            f"Configured cloud-hypervisor binary is invalid or not executable: {CH_BINARY_PATH}"
        )

    resolved = shutil.which("cloud-hypervisor")
    if not resolved:
        raise MicroVMError(
            "cloud-hypervisor binary not found. Set virtualizers.ch.BINARY_PATH or install it in PATH."
        )
    return resolved


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
            raise MicroVMError(
                f"Invalid CONSOLE_MODE value '{console_mode}'. Expected one of off/null/tty/file=<path>."
            )

    return args, serial_log_path


def _kernel_cmdline(vm_ip: str, netmask: str) -> str:
    """The guest's cmdline: family addressing, architecture-determined console."""
    ip_param = network.guest_ip_cmdline_token(vm_ip=vm_ip, netmask=netmask)

    # The console is derived, never configured. It is determined by the
    # architecture (see microvm.guest.serial_device), and naming the wrong one
    # does not degrade the guest, it kills it: /init's first statement redirects
    # to /dev/console, so on arm64 a console=ttyS0 panics PID 1 before it prints
    # anything.
    #
    # A console= in KERNEL_CMDLINE_EXTRA is therefore dropped rather than
    # honoured. config.example.yaml shipped console=ttyS0 with a comment telling
    # operators to keep it, so it is in the config of every node installed before
    # this, and those nodes cannot launch anything until it stops taking effect.
    console = microvm_guest.serial_device()
    cmdline_parts = ["root=/dev/vda", "rw", ip_param, f"console={console}"]

    extra = str(KERNEL_CMDLINE_EXTRA).strip() if KERNEL_CMDLINE_EXTRA is not None else ""
    if extra:
        kept = [tok for tok in extra.split() if not tok.startswith("console=")]
        dropped = [tok for tok in extra.split() if tok.startswith("console=")]
        for tok in dropped:
            if tok != f"console={console}":
                log.LOGGER(
                    f"[CH] ignoring '{tok}' from virtualizers.ch.KERNEL_CMDLINE_EXTRA: "
                    f"this architecture's guest console is {console}."
                )
        if kept:
            cmdline_parts.extend(kept)

    return " ".join(cmdline_parts)


def _build_ch_process_args(start_command: List[str], vmachine_id: str) -> List[str]:
    """Rename the hypervisor process so a recycled PID cannot impersonate this VM.

    ``argv[0]`` becomes the VM's visible name while ``executable=`` still points at
    the real binary; every later reader matches that name (see
    ``microvm.process``) rather than trusting the PID alone.
    """
    visible_name = CH.process_name(vmachine_id)
    return [visible_name, *start_command[1:]] if start_command else [visible_name]


def execute(
    assigment_ports: Optional[Dict[int, int]],
    by_local: bool,
    service_id: str,
    service: celaut.Service,
    config: Optional[celaut.Configuration],
    system_resources: celaut.Service.Container.Resources,
    father_id: str,
    register_instance: Optional[Callable[[str, str, celaut.Sysresources], None]] = None,
) -> Tuple[str, str, celaut.Sysresources]:
    """Boot ``service`` as a microVM and return (vmachine_id, vm_ip, resolved).

    ``register_instance`` is called once, with those same three values, the instant
    the hypervisor process exists -- not when this function returns. A guest starts
    running code while the rest of this function is still waiting for its network,
    applying firewall rules and recording DNAT, and a service's first act is often
    a call back to the node: an observed one asked for ModifyServiceSystemResources
    less than a second after boot. Everything the node knows about a caller it looks
    up by source address, so an instance it has not recorded yet is a caller it
    cannot identify -- and it answered that first call with
    ``Error charging for the resource change of <ip>``, because the charge is where
    the missing row was first noticed. Hence the callback here rather than at the
    call site: only the backend knows when the guest becomes able to speak.

    Only the ``at_init`` end of ``system_resources`` is read here. CH resizes a guest
    by moving its cgroup, which can be raised at any point in the instance's life, so
    there is nothing about the declared ceiling this backend needs to reserve while
    booting -- unlike QEMU, whose ``-m`` is fixed once the process exists.
    """
    initial_system_resources = system_resources.at_init
    vmachine_id = generate_vmachine_id()
    log_prefix = CH.prefix(vmachine_id)
    runtime_dir = paths.runtime_vm_dir(vmachine_id)
    cleanup_rules: List[List[str]] = []
    tap_name: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    rootfs_path = runtime_dir / "rootfs.ext4"
    api_socket_path = _api_socket_path(vmachine_id)
    config_host_path = runtime_dir / "__config__"
    entrypoint_host_path = runtime_dir / ".__nodo_entrypoint"
    stdout_path = runtime_dir / "cloud-hypervisor.stdout.log"
    stderr_path = runtime_dir / "cloud-hypervisor.stderr.log"
    serial_log_path: Optional[Path] = None
    resolved_entrypoint: Optional[str] = None

    try:
        log.LOGGER(f"[CH][{vmachine_id}] event=start")
        log.LOGGER(
            f"[CH][{vmachine_id}] execute start: service_id={service_id}, father_id={father_id}, "
            f"by_local={by_local}, assignment_ports={assigment_ports}, cache={paths.cache_root()}, "
            f"bridge={network.NETWORK_BRIDGE_NAME}, subnet={network.NETWORK_SUBNET}, "
            f"gateway={network.NETWORK_GATEWAY_IP}"
        )

        log.LOGGER(f"[CH][{vmachine_id}] running network preflight")
        guest_network = network.preflight()
        log.LOGGER(f"[CH][{vmachine_id}] network preflight ok: {guest_network.with_prefixlen}")

        ch_binary = _resolve_ch_binary()
        log.LOGGER(f"[CH][{vmachine_id}] cloud-hypervisor binary resolved: {ch_binary}")

        arch = microvm_bundle.resolve_service_arch(service_id=service_id, service=service)
        bundle = microvm_bundle.load_bundle(service_id=service_id, arch=arch)
        log.LOGGER(
            f"[CH][{vmachine_id}] bundle loaded: arch={bundle['arch']}, "
            f"rootfs={bundle['rootfs_path']}, kernel={bundle['kernel_path']}, "
            f"initramfs={bundle['initramfs_path']}"
        )
        microvm_bundle.validate_custom_initramfs(bundle["initramfs_path"])
        log.LOGGER(f"[CH][{vmachine_id}] initramfs validation passed for {bundle['initramfs_path']}")

        resolved_entrypoint = microvm_bundle.validate_entrypoint_strict(service=service)
        log.LOGGER(f"[CH][{vmachine_id}] validated strict entrypoint: {resolved_entrypoint}")

        vm_ip, mac = network.deterministic_ip_and_mac(vmachine_id)
        log.LOGGER(f"[CH][{vmachine_id}] deterministic networking: ip={vm_ip}, mac={mac}")

        runtime_dir.mkdir(parents=True, exist_ok=True)
        log.LOGGER(f"[CH][{vmachine_id}] runtime dir prepared: {runtime_dir}")
        api_socket_path.parent.mkdir(parents=True, exist_ok=True)
        log.LOGGER(f"[CH][{vmachine_id}] API socket dir prepared: {api_socket_path.parent}")

        try:
            api_socket_path.unlink(missing_ok=True)
        except Exception as e:
            raise MicroVMError(
                f"Unable to prepare API socket path {api_socket_path}: {e}"
            ) from e

        shutil.copy2(bundle["rootfs_path"], rootfs_path)
        log.LOGGER(f"[CH][{vmachine_id}] rootfs copied to runtime image: {rootfs_path}")

        network_resolution = rootfs.build_network_resolution(service=service, father_id=father_id, config=config)
        log.LOGGER(f"[CH][{vmachine_id}] network resolution entries: {len(network_resolution)}")
        cfg = rootfs.build_configuration_file(
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

        config_targets = rootfs.guest_config_targets(service=service)
        log.LOGGER(
            f"[CH][{vmachine_id}] guest config targets={config_targets} "
            f"(service.container.config_declaration.path={list(service.container.config_declaration.path)})"
        )
        for target_path in config_targets:
            log.LOGGER(f"[CH][{vmachine_id}] injecting config into guest target: {target_path}")
            rootfs.debugfs_write(
                image_path=rootfs_path,
                host_file=config_host_path,
                guest_target=target_path,
            )
        log.LOGGER(f"[CH][{vmachine_id}] guest config injection completed for {len(config_targets)} target(s)")

        with open(entrypoint_host_path, "w", encoding="utf-8") as f:
            f.write(f"{resolved_entrypoint}\n")
        log.LOGGER(f"[CH][{vmachine_id}] entrypoint metadata serialized: {entrypoint_host_path}")
        rootfs.debugfs_write(
            image_path=rootfs_path,
            host_file=entrypoint_host_path,
            guest_target=rootfs.GUEST_ENTRYPOINT_PATH,
        )
        log.LOGGER(
            f"[CH][{vmachine_id}] guest entrypoint injection completed: "
            f"{rootfs.GUEST_ENTRYPOINT_PATH}"
        )

        # Shared filesystems (parent -> child inheritance). A service exports its
        # `shared=true` directories to the children it launches, and inherits its
        # parent's exports for its own `guest=true` directories. The exporting
        # parent owns the share (share id = H(this_instance_id, path)); a child
        # reconstructs the same id from its `father_id`, so it can only attach to a
        # directory its own parent exported. VirtioFS is the backend here only —
        # the service spec never mentions it. Ordinary services declare neither and
        # this whole block is a no-op.
        shared_fs_dir = str(shared_fs_base_dir(paths.cache_root()))
        virtiofs_mounts = []
        exported_share_ids: List[str] = []
        if shared_fs_dir:
            export_mounts = parent_export_mounts(service, vmachine_id, shared_fs_dir)
            guest_mounts = child_guest_mounts(service, father_id, shared_fs_dir)
            share_mounts = export_mounts + guest_mounts
            if share_mounts:
                log.LOGGER(
                    f"[CH][{vmachine_id}] shared filesystems: {len(export_mounts)} exported, "
                    f"{len(guest_mounts)} inherited from father={father_id}"
                )
                fs_device_args, virtiofs_mounts, _ = attach_virtiofs_backends(
                    share_mounts,
                    base_dir=shared_fs_dir,
                    socket_dir=str(paths.control_socket_dir()),
                    virtiofsd_binary=VIRTIOFSD_BINARY,
                    logger_fn=log.LOGGER,
                )
                exported_share_ids = [m.share_id_hex for m in export_mounts]
                mount_plan_host_path = runtime_dir / ".__nodo_virtiofs"
                with open(mount_plan_host_path, "w", encoding="utf-8") as f:
                    f.write(build_guest_mount_plan(share_mounts))
                rootfs.debugfs_write(
                    image_path=rootfs_path,
                    host_file=mount_plan_host_path,
                    guest_target=GUEST_MOUNT_PLAN_PATH,
                )
                log.LOGGER(
                    f"[CH][{vmachine_id}] virtiofs devices attached: {len(share_mounts)}; "
                    f"guest mount plan injected: {GUEST_MOUNT_PLAN_PATH}"
                )
            else:
                fs_device_args = []
        else:
            fs_device_args = []

        tap_name = network.create_tap(vmachine_id)
        log.LOGGER(f"[CH][{vmachine_id}] TAP created and attached: {tap_name}")
        network.log_host_network_probe(log_prefix=log_prefix, vm_ip=vm_ip, tap_name=tap_name)

        # Committed to the FORWARD chain before the guest exists, not after it
        # starts pinging. Nothing this needs -- vm_ip, the gateway's address and
        # port, network_resolution -- depends on the guest being alive; every one
        # of them was already resolved above, to build this VM's own config. The
        # tap is enslaved to the bridge as of the line above, so it is already
        # forwarding-capable: a policy applied any later (this used to run after
        # `_wait_guest_network_ready`, seconds from now) left a real window in
        # which a booting guest's traffic answered only to the host's own default
        # FORWARD policy -- unrestricted on a plain install -- instead of nodo's
        # allow-list. This is that policy's only chance to be in place before a
        # single packet could have been forwarded.
        network.configure_guest_firewall_policy(
            log_prefix=log_prefix,
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            network_resolution=network_resolution,
        )

        vcpus, mem_b, cpu_quota, cpu_period = limits.resolve_initial_resources(initial_system_resources)
        # The row must record what was resolved here -- the values actually enforced
        # on the guest (cgroup cpu.max + VM memory size) below -- not what the
        # manifest requested. Persisting the manifest is what billed instances for
        # what they asked rather than for what they hold (#249).
        #
        # Disk is the size of the rootfs image this instance was just given, which is
        # the manifest's `disk_space` only when that figure happened to be the largest
        # of the three inputs to `limits.initial_rootfs_size_bytes` -- the build also
        # floors it at MIN_ROOTFS_BYTES, at the populated tree plus overhead, and grows
        # it further whenever mkfs.ext4 ran out of space. Every one of those makes the
        # instance hold more disk than it asked for, and the manifest figure would bill
        # it for the smaller number.
        disk_b = rootfs.runtime_disk_bytes(log_prefix=log_prefix, rootfs_path=rootfs_path)
        resolved_resources = celaut.Sysresources(
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            mem_limit=mem_b,
            disk_space=disk_b,
        )
        # The VM is booted larger than the figure above: `mem_b` is memory the
        # *service* may use, and the guest kernel's own footprint (text, percpu, one
        # struct page per frame) comes out of the VM's RAM before init runs. Sizing
        # the VM at `mem_b` hands the service less than its manifest declared and
        # OOM-kills it below its own ceiling. Only the boot argument grows; the row
        # and the price stay at `mem_b`, so the node absorbs the kernel rather than
        # billing the client for it.
        #
        # The reserve is per-architecture: `arch` here is the guest's, resolved above
        # from the service's own manifest, not the host's. On the CH path they are
        # always the same (KVM cannot run a foreign guest), but passing it explicitly
        # keeps the two backends reading the same figure from the same place.
        boot_mem_b = limits.guest_boot_memory_bytes(mem_b, arch=arch)
        # What separates the VM's RAM size from the usable bytes the row records.
        # Persisted in the runtime state below: this backend's resize knob is the
        # cgroup, so every later memory change has to add the same figure back to
        # keep bounding the boot allocation rather than the usable one.
        guest_kernel_reserve_b = boot_mem_b - mem_b
        mem_mib = math.ceil(boot_mem_b / (1024 * 1024))
        netmask = str(guest_network.netmask)
        kernel_cmdline = _kernel_cmdline(vm_ip=vm_ip, netmask=netmask)
        log.LOGGER(
            f"[CH][{vmachine_id}] VM resources: vcpus={vcpus}, mem_mib={mem_mib} "
            f"(usable target {math.ceil(mem_b / (1024 * 1024))} MiB + "
            f"{math.ceil(guest_kernel_reserve_b / (1024 * 1024))} MiB {arch} guest kernel reserve), "
            f"guest_net_device={network.GUEST_NET_DEVICE}, kernel_cmdline={kernel_cmdline}"
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
        start_command.extend(fs_device_args)
        start_command.extend(stream_args)
        log.LOGGER(f"[CH][{vmachine_id}] launching cloud-hypervisor: {' '.join(start_command)}")

        process_args = _build_ch_process_args(
            start_command=start_command,
            vmachine_id=vmachine_id,
        )

        with open(stdout_path, "w", encoding="utf-8") as stdout_file, open(
            stderr_path, "w", encoding="utf-8"
        ) as stderr_file:
            process = subprocess.Popen(
                process_args,
                executable=ch_binary,
                stdout=stdout_file,
                stderr=stderr_file,
            )
        log.LOGGER(
            f"[CH][{vmachine_id}] process started: pid={process.pid}, visible_name={process_args[0]}, "
            f"api_socket={api_socket_path}, "
            f"stdout={stdout_path}, stderr={stderr_path}"
        )

        # From here the guest is running: the kernel is booting and the service on
        # it can reach the gateway before this function does anything else. So the
        # node's two records of it are written now, before the health check's own
        # second of sleep, and not at the end of the launch.
        save_booting_state(
            vmachine_id,
            hypervisor=CH,
            service_id=service_id,
            pid=process.pid,
            ip=vm_ip,
            mac=mac,
            tap=tap_name,
            bridge=network.NETWORK_BRIDGE_NAME,
            cleanup_rules=cleanup_rules,
            rule_comment_prefix=fw_policy.vm_comment_prefix(vmachine_id),
        )
        if register_instance:
            register_instance(vmachine_id, vm_ip, resolved_resources)
            log.LOGGER(f"[CH][{vmachine_id}] instance registered before the guest could call in")

        time.sleep(1.0)
        if process.poll() is not None:
            raise MicroVMError(
                f"cloud-hypervisor process exited early with code {process.returncode}. "
                f"See {stderr_path}. stderr tail: {serial.tail_file(stderr_path)}"
            )
        log.LOGGER(f"[CH][{vmachine_id}] process health check passed after 1s")

        # Set cgroup limits
        vm_cgroup: Path = ensure_vm_cgroup(vmachine_id=vmachine_id, pid=process.pid)
        # The cgroup caps the hypervisor *process*, so it has to bound what the VM
        # was actually booted with. Capping it at `mem_b` while the guest holds
        # `boot_mem_b` would have the host kill the VM for using the RAM the node
        # itself gave it.
        apply_memory_limit(vm_cgroup=vm_cgroup, mem_limit=boot_mem_b)
        apply_cpu_limit(vm_cgroup=vm_cgroup, cpu_quota=cpu_quota, cpu_period=cpu_period)

        # Network
        network_timeout_s = network.ready_timeout_seconds(GUEST_NETWORK_READY_TIMEOUT_S)
        log.LOGGER(
            f"[CH][{vmachine_id}] waiting guest network readiness: vm_ip={vm_ip}, timeout={network_timeout_s}s"
        )
        network.wait_guest_network_ready(
            log_prefix=log_prefix,
            vm_ip=vm_ip,
            timeout_s=network_timeout_s,
            serial_log_path=serial_log_path,
        )
        log.LOGGER(f"[CH][{vmachine_id}] event=ready")
        network.log_host_network_probe(log_prefix=log_prefix, vm_ip=vm_ip, tap_name=tap_name)

        dnat_rules_state: List[Dict[str, object]] = []
        if not by_local and assigment_ports:
            slot_by_port = {slot.port: slot for slot in service.api.slot}
            for internal_port, external_port in assigment_ports.items():
                slot = slot_by_port.get(internal_port)
                if not slot:
                    log.LOGGER(
                        f"[CH][{vmachine_id}] skipping DNAT for internal_port={internal_port}: "
                        "slot not found in service.api.slot"
                    )
                    continue

                protocol = resolve_slot_transport_protocols(
                    slot,
                    logger_fn=log.LOGGER,
                    context=f"[CH][{vmachine_id}]",
                )
                if not protocol:
                    log.LOGGER(
                        f"[CH][{vmachine_id}] skipping DNAT for internal_port={internal_port}: "
                        "no host-supported transports in slot.transport.tags"
                    )
                    continue

                network.add_dnat_rule(
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
                    f"[CH][{vmachine_id}] DNAT rule added: {protocol.value} "
                    f"host:{external_port} -> guest:{vm_ip}:{internal_port}"
                )

        save_runtime_state(
            vmachine_id,
            {
                "vmachine_id": vmachine_id,
                # The index the janitor and the health check read. Written again
                # here and not only in the booting state: this write replaces that
                # one wholesale, and a final state that dropped `virtualizer`
                # left every later reader guessing which backend owned the guest
                # -- which is the guess that reaped a healthy VM (#295).
                "virtualizer": CH.name,
                "process_name": CH.process_name(vmachine_id),
                "service_id": service_id,
                "arch": bundle["arch"],
                "pid": process.pid,
                "control_socket": str(api_socket_path),
                "tap": tap_name,
                "ip": vm_ip,
                "mac": mac,
                "rootfs_path": str(rootfs_path),
                "entrypoint": resolved_entrypoint,
                "dnat_rules": dnat_rules_state,
                "cleanup_rules": cleanup_rules,
                "rule_comment_prefix": fw_policy.vm_comment_prefix(vmachine_id),
                "cgroup_path": vm_cgroup.as_posix(),
                # The guest kernel's footprint inside this VM's RAM size, measured at
                # boot. A memory resize adds it back to the usable figure it is asked
                # for, so memory.max keeps bounding what the VM was booted with.
                # Absent on an instance launched before the reserve existed, which was
                # booted at exactly its usable figure and so has none.
                "guest_kernel_reserve_bytes": guest_kernel_reserve_b,
                "virtiofs": virtiofs_mounts,
                "exported_shares": exported_share_ids,
                "bridge": network.NETWORK_BRIDGE_NAME,
                "serial_log": str(serial_log_path) if serial_log_path else "",
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        log.LOGGER(
            f"[CH][{vmachine_id}] runtime state persisted: "
            f"{paths.runtime_state_file(vmachine_id)}"
        )

        log.LOGGER(
            f"Cloud Hypervisor VM started: {vmachine_id} ({vm_ip}), runtime_dir={runtime_dir}"
        )
        return vmachine_id, vm_ip, resolved_resources

    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] execute failed: {type(e).__name__}: {e}")
        log.LOGGER(f"[CH][{vmachine_id}] traceback:\n{traceback.format_exc()}")
        if process:
            log.LOGGER(
                f"[CH][{vmachine_id}] process state at failure: pid={process.pid}, poll={process.poll()}"
            )
        log.LOGGER(f"[CH][{vmachine_id}] stdout tail ({stdout_path}): {serial.tail_file(stdout_path)}")
        log.LOGGER(f"[CH][{vmachine_id}] stderr tail ({stderr_path}): {serial.tail_file(stderr_path)}")
        if serial_log_path:
            log.LOGGER(
                f"[CH][{vmachine_id}] serial tail ({serial_log_path}): "
                f"{serial.tail_file(serial_log_path)}"
            )

        if cleanup_rules:
            log.LOGGER(f"[CH][{vmachine_id}] removing {len(cleanup_rules)} cleanup firewall rules")
        network.replay_legacy_cleanup_rules(cleanup_rules)
        # Whatever was applied before the failure carries this VM's prefix.
        try:
            vm_remove_vm_rules(vmachine_id)
        except Exception as e:
            log.LOGGER(f"[CH][{vmachine_id}] could not remove this VM's firewall rules: {e}")

        if process and process.poll() is None:
            log.LOGGER(f"[CH][{vmachine_id}] terminating cloud-hypervisor process pid={process.pid}")
            process.terminate()
            time.sleep(0.5)
            if process.poll() is None:
                log.LOGGER(f"[CH][{vmachine_id}] killing cloud-hypervisor process pid={process.pid}")
                process.kill()

        if tap_name:
            log.LOGGER(f"[CH][{vmachine_id}] deleting TAP interface: {tap_name}")
            network.delete_tap(tap_name)

        log.LOGGER(f"[CH][{vmachine_id}] deleting runtime state entry")
        delete_runtime_state(vmachine_id)

        if runtime_dir.exists():
            if CONSERVE_RUNTIME_DIR_ON_FAILURE:

                log.LOGGER(f"[CH][{vmachine_id}] preserving runtime directory for debugging: {runtime_dir}")
                failures_dir = paths.failures_root()
                failures_dir.mkdir(parents=True, exist_ok=True)
                
                target = failures_dir / vmachine_id
                shutil.move(str(runtime_dir), str(target))

                with open(target / "error.txt", "w") as f:
                    f.write(str(e))
                    f.write("\n\n")
                    f.write(traceback.format_exc())

            else:
                log.LOGGER(f"[CH][{vmachine_id}] removing runtime directory: {runtime_dir}")
                shutil.rmtree(runtime_dir, ignore_errors=True)

        if isinstance(e, MicroVMError):
            raise
        raise MicroVMError(str(e)) from e
