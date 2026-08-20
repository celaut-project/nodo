import os
import platform
import pwd
import grp
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def _parse_unit_user(unit_content: str) -> str:
    match = re.search(r"^\s*User\s*=\s*(\S+)\s*$", unit_content, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return "root"


def _run_quiet(command):
    return subprocess.run(command, capture_output=True, text=True)


def _can_access_kvm_as_user(username: str) -> bool:
    test_cmd = ["test", "-r", "/dev/kvm", "-a", "-w", "/dev/kvm"]
    runner_variants = [
        ["runuser", "-u", username, "--"] + test_cmd,
        ["su", "-s", "/bin/sh", username, "-c", "test -r /dev/kvm -a -w /dev/kvm"],
    ]
    for command in runner_variants:
        try:
            result = _run_quiet(command)
            if result.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _user_in_kvm_group(username: str) -> bool:
    try:
        user_info = pwd.getpwnam(username)
    except KeyError:
        return False

    try:
        kvm_group = grp.getgrnam("kvm")
    except KeyError:
        return False

    if user_info.pw_gid == kvm_group.gr_gid:
        return True

    try:
        user_groups = [g.gr_name for g in grp.getgrall() if username in g.gr_mem]
    except Exception:
        user_groups = []
    return "kvm" in user_groups


def _doctor_kvm_checks(service_user: str):
    print("\nVirtualization checks (Cloud Hypervisor/KVM):", flush=True)

    vmx_count = 0
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as cpuinfo:
            content = cpuinfo.read()
            vmx_count = len(re.findall(r"\b(vmx|svm)\b", content))
    except Exception:
        pass

    if vmx_count > 0:
        print(f"[OK] CPU virtualization flags detected (vmx/svm matches: {vmx_count}).", flush=True)
    else:
        print("[WARN] No vmx/svm CPU flags detected in /proc/cpuinfo.", flush=True)

    modules_text = ""
    try:
        with open("/proc/modules", "r", encoding="utf-8", errors="replace") as modules_file:
            modules_text = modules_file.read()
    except Exception:
        pass

    if re.search(r"^kvm(\s|_)", modules_text, flags=re.MULTILINE):
        print("[OK] KVM kernel modules appear to be loaded.", flush=True)
    else:
        print("[WARN] KVM kernel modules not detected in /proc/modules.", flush=True)

    if not os.path.exists("/dev/kvm"):
        print("[FAIL] /dev/kvm does not exist on this host.", flush=True)
        return

    try:
        stat_info = os.stat("/dev/kvm")
        owner = pwd.getpwuid(stat_info.st_uid).pw_name
        group = grp.getgrgid(stat_info.st_gid).gr_name
        perms = oct(stat_info.st_mode & 0o777)
        print(f"[OK] /dev/kvm exists (owner={owner}, group={group}, mode={perms}).", flush=True)
    except Exception:
        print("[OK] /dev/kvm exists.", flush=True)

    if service_user == "root":
        print("[OK] Service user is root; /dev/kvm access is usually available.", flush=True)
        return

    if _can_access_kvm_as_user(service_user):
        print(f"[OK] Service user '{service_user}' can read/write /dev/kvm.", flush=True)
        return

    print(f"[FAIL] Service user '{service_user}' cannot read/write /dev/kvm.", flush=True)
    if not _user_in_kvm_group(service_user):
        print(
            f"  Suggestion: add '{service_user}' to group 'kvm' and restart session/service.",
            flush=True,
        )
        print(f"  Command: sudo usermod -aG kvm {service_user}", flush=True)
    else:
        print(
            "  The user is in group 'kvm', so check ACLs, container device passthrough, or unit sandboxing.",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Cloud Hypervisor compatibility checks
# ---------------------------------------------------------------------------

def _resolve_config_paths(main_dir: str):
    """Load minimal CH-related paths from config.yaml without importing ConfigManager."""
    import yaml

    config_path = os.path.join(main_dir, "config.yaml")
    if not os.path.isfile(config_path):
        return {}

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    main_cfg = raw.get("main", {})
    main_dir_cfg = main_cfg.get("MAIN_DIR", main_dir)
    ch_cfg = raw.get("virtualizers", {}).get("ch", {})

    def _interpolate(value):
        if not isinstance(value, str):
            return value
        return value.replace("${main.MAIN_DIR}", str(main_dir_cfg))

    return {
        "binary_path": _interpolate(ch_cfg.get("BINARY_PATH", "")),
        "kernel_paths": {
            k: _interpolate(v)
            for k, v in (ch_cfg.get("KERNEL_PATHS") or {}).items()
        },
        "initramfs_paths": {
            k: _interpolate(v)
            for k, v in (ch_cfg.get("INITRAMFS_PATHS") or {}).items()
        },
        "main_dir": str(main_dir_cfg),
    }


def _get_nested_config(raw, path: str, default):
    value = raw
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _expand_main_dir_placeholder(value, main_dir: str):
    if not isinstance(value, str):
        return value
    return value.replace("${main.MAIN_DIR}", main_dir)


def _resolve_admin_group() -> str:
    """Pick the unit's Group the same way install.sh does.

    The admin group is distro-specific — `sudo` on Debian, `wheel` on Fedora/RHEL —
    and systemd refuses to start a unit whose Group cannot be resolved. Must stay in
    sync with create_service_file() in install.sh, or doctor rewrites the unit on
    every run (and leaves the service stopped).
    """
    import grp

    for name in ("sudo", "wheel"):
        try:
            grp.getgrnam(name)
            return name
        except KeyError:
            continue
    return "root"


def _render_service_template(template_content: str, main_dir: str) -> str:
    """Render nodo.service.template using the same config keys as install.sh."""
    import yaml

    config_path = os.path.join(main_dir, "config.yaml")
    raw = {}
    if os.path.isfile(config_path):
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}

    java_home = _expand_main_dir_placeholder(
        _get_nested_config(
            raw,
            "dependencies.java.JAVA_HOME",
            os.path.join(main_dir, "runtime", "java", "current"),
        ),
        main_dir,
    )
    python_runtime_bin = _expand_main_dir_placeholder(
        _get_nested_config(
            raw,
            "dependencies.python.RUNTIME_BIN",
            os.path.join(main_dir, "runtime", "python", "current", "bin", "python3"),
        ),
        main_dir,
    )
    python_venv_bin = _expand_main_dir_placeholder(
        _get_nested_config(
            raw,
            "dependencies.python.VENV_BIN",
            os.path.join(main_dir, "venv", "bin", "python"),
        ),
        main_dir,
    )

    replacements = {
        "{{MAIN_DIR}}": main_dir,
        "{{JAVA_HOME}}": java_home,
        "{{PYTHON_RUNTIME_BIN_DIR}}": os.path.dirname(python_runtime_bin),
        "{{PYTHON_VENV_BIN}}": python_venv_bin,
        "{{ADMIN_GROUP}}": _resolve_admin_group(),
    }

    rendered = template_content
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)

    unresolved = sorted(set(re.findall(r"{{[A-Z_]+}}", rendered)))
    if unresolved:
        raise ValueError(
            "Unresolved placeholders in nodo.service.template: " + ", ".join(unresolved)
        )

    return rendered


def _get_host_arch_tag() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    return f"linux/{machine}"


def _parse_kernel_version(release: str):
    """Extract (major, minor) ints from a kernel release string like '6.17.0-19-generic'."""
    match = re.match(r"(\d+)\.(\d+)", release)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def _doctor_ch_binary(ch_binary: str):
    """Check cloud-hypervisor binary exists, is executable, and report its version."""
    print("\nCloud Hypervisor binary:", flush=True)

    if not ch_binary:
        resolved = shutil.which("cloud-hypervisor")
        if resolved:
            ch_binary = resolved
        else:
            print("[FAIL] No cloud-hypervisor binary configured and none found in PATH.", flush=True)
            return None

    if not os.path.isfile(ch_binary):
        print(f"[FAIL] Cloud Hypervisor binary not found at: {ch_binary}", flush=True)
        return None

    if not os.access(ch_binary, os.X_OK):
        print(f"[FAIL] Cloud Hypervisor binary is not executable: {ch_binary}", flush=True)
        return None

    print(f"[OK] Cloud Hypervisor binary found: {ch_binary}", flush=True)

    # Get version
    try:
        result = subprocess.run(
            [ch_binary, "--version"], capture_output=True, text=True, timeout=10
        )
        version_text = (result.stdout or "").strip() or (result.stderr or "").strip()
        if version_text:
            print(f"[OK] Cloud Hypervisor version: {version_text}", flush=True)
        else:
            print("[WARN] Could not determine Cloud Hypervisor version.", flush=True)
    except Exception as e:
        print(f"[WARN] Could not query Cloud Hypervisor version: {e}", flush=True)
        version_text = ""

    return ch_binary


def _doctor_host_kernel():
    """Check host kernel version and warn about known-incompatible kernels."""
    print("\nHost kernel compatibility:", flush=True)

    release = platform.release()
    major, minor = _parse_kernel_version(release)
    print(f"[INFO] Host kernel: {release}", flush=True)

    if major is not None and minor is not None:
        # Kernels >= 6.13 may introduce KVM exit reason changes that break
        # older Cloud Hypervisor builds.  6.17+ is experimentally bleeding-edge.
        if major > 6 or (major == 6 and minor >= 17):
            print(
                f"[WARN] Kernel {major}.{minor} is bleeding-edge. Cloud Hypervisor may fail with "
                "'VcpuRun InternalError' if the CH binary does not support the KVM changes "
                "introduced in this kernel.",
                flush=True,
            )
            print(
                "  Suggestion: Upgrade Cloud Hypervisor to the latest release, or "
                "use a stable kernel (e.g. 6.8, 6.11, 6.12 LTS).",
                flush=True,
            )
        elif major == 6 and minor >= 13:
            print(
                f"[WARN] Kernel {major}.{minor} includes KVM changes that may affect "
                "Cloud Hypervisor compatibility. Verify with the smoke test below.",
                flush=True,
            )
        else:
            print(f"[OK] Kernel {major}.{minor} is expected to be compatible.", flush=True)
    else:
        print("[WARN] Could not parse kernel version. Compatibility cannot be assessed.", flush=True)

    return release


def _doctor_guest_kernel(kernel_paths: dict, host_arch_tag: str):
    """Check guest kernel (vmlinuz) exists for the host architecture."""
    print("\nGuest kernel (vmlinuz):", flush=True)

    kernel_path = kernel_paths.get(host_arch_tag, "")
    if not kernel_path:
        print(
            f"[FAIL] No guest kernel path configured for architecture '{host_arch_tag}'.",
            flush=True,
        )
        return None

    if not os.path.isfile(kernel_path):
        print(f"[FAIL] Guest kernel not found at: {kernel_path}", flush=True)
        print("  Suggestion: Re-run the installer to provision kernel assets.", flush=True)
        return None

    size = os.path.getsize(kernel_path)
    print(f"[OK] Guest kernel found: {kernel_path} ({size} bytes)", flush=True)

    # Basic sanity: vmlinuz should be at least 1 MiB for a real kernel
    if size < 1024 * 1024:
        print(
            f"[WARN] Guest kernel is suspiciously small ({size} bytes). "
            "It may be corrupt or truncated.",
            flush=True,
        )

    return kernel_path


def _doctor_initramfs(initramfs_paths: dict, host_arch_tag: str):
    """Check custom initramfs exists and contains required entries."""
    print("\nCustom initramfs:", flush=True)

    initramfs_path = initramfs_paths.get(host_arch_tag, "")
    if not initramfs_path:
        print(
            f"[FAIL] No initramfs path configured for architecture '{host_arch_tag}'.",
            flush=True,
        )
        return None

    if not os.path.isfile(initramfs_path):
        print(f"[FAIL] Initramfs not found at: {initramfs_path}", flush=True)
        print("  Suggestion: Re-run the installer to regenerate the initramfs.", flush=True)
        return None

    size = os.path.getsize(initramfs_path)
    print(f"[OK] Initramfs found: {initramfs_path} ({size} bytes)", flush=True)

    # Verify contents with lsinitramfs if available
    lsinitramfs = shutil.which("lsinitramfs")
    if lsinitramfs:
        try:
            result = subprocess.run(
                [lsinitramfs, initramfs_path], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                entries = {
                    line.strip().lstrip("./")
                    for line in (result.stdout or "").splitlines()
                    if line.strip()
                }
                required = {"init", "bin/busybox", "etc/nodo-ch-initramfs.marker"}
                missing = sorted(required - entries)
                if missing:
                    print(
                        f"[FAIL] Initramfs is missing required entries: {missing}",
                        flush=True,
                    )
                    print(
                        "  Suggestion: Re-run the installer to regenerate the initramfs.",
                        flush=True,
                    )
                else:
                    print("[OK] Initramfs contains all required entries (init, busybox, marker).", flush=True)
            else:
                print(
                    f"[WARN] lsinitramfs returned error ({result.returncode}). "
                    "Cannot verify initramfs contents.",
                    flush=True,
                )
        except Exception as e:
            print(f"[WARN] Could not inspect initramfs: {e}", flush=True)
    else:
        print("[WARN] lsinitramfs not found; skipping content verification.", flush=True)

    return initramfs_path


def _doctor_ch_smoke_test(ch_binary: str, kernel_path: str, initramfs_path: str):
    """Run a minimal Cloud Hypervisor VM to verify vCPU execution works on this host.

    This catches incompatibilities between the CH binary and the host kernel's
    KVM implementation (e.g. the 'Unexpected exit reason on vcpu run: InternalError').
    """
    print("\nCloud Hypervisor KVM smoke test:", flush=True)

    if not ch_binary or not kernel_path or not initramfs_path:
        print("[SKIP] Smoke test skipped — missing binary, kernel, or initramfs.", flush=True)
        return

    if not os.path.exists("/dev/kvm"):
        print("[SKIP] Smoke test skipped — /dev/kvm not available.", flush=True)
        return

    tmpdir = tempfile.mkdtemp(prefix="nodo-doctor-ch-smoke-")
    try:
        # Create a minimal ext4 rootfs image (just needs to be mountable)
        rootfs_path = os.path.join(tmpdir, "rootfs.ext4")
        api_socket = os.path.join(tmpdir, "ch.sock")
        stderr_log = os.path.join(tmpdir, "ch.stderr.log")
        serial_log = os.path.join(tmpdir, "ch.serial.log")

        # Create a 16 MiB empty ext4 image
        try:
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={rootfs_path}", "bs=1M", "count=16"],
                capture_output=True, check=True
            )
            subprocess.run(
                ["mkfs.ext4", "-F", "-q", rootfs_path],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[SKIP] Cannot create test rootfs: {e}", flush=True)
            return

        cmdline = "root=/dev/vda rw console=ttyS0"

        cmd = [
            ch_binary,
            "--api-socket", api_socket,
            "--kernel", kernel_path,
            "--initramfs", initramfs_path,
            "--disk", f"path={rootfs_path},image_type=raw",
            "--cpus", "boot=1",
            "--memory", "size=64M",
            "--cmdline", cmdline,
            "--serial", f"file={serial_log}",
            "--console", "off",
        ]

        with open(stderr_log, "w") as stderr_f:
            process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=stderr_f
            )

        # Wait a bit for the VM to start (or fail)
        time.sleep(2.0)

        poll = process.poll()

        # Read stderr output
        stderr_content = ""
        try:
            with open(stderr_log, "r", errors="replace") as f:
                stderr_content = f.read()
        except Exception:
            pass

        serial_content = ""
        try:
            with open(serial_log, "r", errors="replace") as f:
                serial_content = f.read()
        except Exception:
            pass

        if poll is not None:
            # Process exited — this is a problem
            if "VcpuRun" in stderr_content or "InternalError" in stderr_content:
                print(
                    "[FAIL] Cloud Hypervisor vCPU failed to execute. The CH binary is "
                    "incompatible with this host kernel's KVM implementation.",
                    flush=True,
                )
                print(f"  stderr: {stderr_content.strip()[:500]}", flush=True)
                print(
                    "  Suggestion: Update Cloud Hypervisor to the latest version, or "
                    "downgrade the host kernel to a stable release (e.g. 6.8, 6.11, 6.12 LTS).",
                    flush=True,
                )
            elif "Vmm(VmCreate(KernelLoad" in stderr_content:
                print(
                    "[FAIL] Cloud Hypervisor could not load the guest kernel. "
                    "The vmlinuz file may be incompatible or corrupt.",
                    flush=True,
                )
                print(f"  stderr: {stderr_content.strip()[:500]}", flush=True)
                print(
                    "  Suggestion: Re-install Nodo or replace the guest kernel with a "
                    "known-good vmlinuz for this architecture.",
                    flush=True,
                )
            else:
                print(
                    f"[FAIL] Cloud Hypervisor exited early with code {poll}.",
                    flush=True,
                )
                print(f"  stderr: {stderr_content.strip()[:500]}", flush=True)
        else:
            # VM is still running — vCPU works!
            # Check that serial has output (kernel is actually booting)
            if serial_content.strip():
                print(
                    "[OK] Cloud Hypervisor vCPU is running. Guest kernel boot detected via serial output.",
                    flush=True,
                )
            else:
                print(
                    "[OK] Cloud Hypervisor vCPU is running (process alive after 2s).",
                    flush=True,
                )

            # Terminate the test VM
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    except Exception as e:
        print(f"[WARN] Smoke test encountered an error: {e}", flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _doctor_cloud_hypervisor(main_dir: str):
    """Run all Cloud Hypervisor compatibility checks."""
    cfg = _resolve_config_paths(main_dir)
    if not cfg:
        print("\n[WARN] Could not load config.yaml; skipping Cloud Hypervisor checks.", flush=True)
        return

    host_arch_tag = _get_host_arch_tag()
    print(f"\nHost architecture: {host_arch_tag}", flush=True)

    ch_binary = _doctor_ch_binary(cfg.get("binary_path", ""))
    _doctor_host_kernel()
    guest_kernel = _doctor_guest_kernel(cfg.get("kernel_paths", {}), host_arch_tag)
    initramfs = _doctor_initramfs(cfg.get("initramfs_paths", {}), host_arch_tag)
    _doctor_ch_smoke_test(ch_binary, guest_kernel, initramfs)


def _doctor_network_checks():
    """Report what can be checked about inbound reachability, from here.

    Deliberately modest: nothing this host can do proves the gateway port is
    reachable from the Internet — a connect from inside succeeds whether or not the
    router forwards anything. So this reports the facts it can establish and defers
    the instructions to ``nodo nat-guide`` instead of duplicating them.
    """
    print("\nInbound reachability:", flush=True)

    try:
        from src.commands.nat_guide import collect_facts

        facts = collect_facts()
    except Exception as e:
        print(f"[WARN] Could not gather network facts: {e}", flush=True)
        return

    port = facts.get("gateway_port")
    if port:
        print(f"[OK] Gateway port resolves to {port}.", flush=True)
    else:
        print("[FAIL] network.GATEWAY_PORT is not resolvable; configure it first.", flush=True)

    public_port = facts.get("public_tcp_port")
    if port and public_port and public_port != port:
        print(
            f"[OK] network.PUBLIC_TCP_PORT is set: router should forward external "
            f"port {public_port} to this host's port {port}.",
            flush=True
        )

    listening = facts.get("listening")
    if listening is True:
        print(f"[OK] Something is listening on 127.0.0.1:{port}.", flush=True)
    elif listening is False:
        print(
            f"[WARN] Nothing is listening on 127.0.0.1:{port}. Start the node with "
            "'nodo daemon start' before testing from outside.",
            flush=True
        )

    if facts.get("ddns_enabled"):
        hostname = facts.get("ddns_hostname") or "(no domain set)"
        resolves = facts.get("ddns_resolves_to")
        if resolves:
            print(f"[OK] DDNS {hostname} resolves to {resolves}.", flush=True)
            if resolves == facts.get("local_ip"):
                print(
                    "[WARN] It resolves to this machine's own LAN address, so peers "
                    "outside your network cannot use it. That usually means the "
                    "provider recorded a private source address.",
                    flush=True
                )
        else:
            print(
                f"[WARN] DDNS is enabled but {hostname} does not resolve. Check "
                "ddns.DOMAIN / ddns.TOKEN and the [DDNS] lines in the node log.",
                flush=True
            )
    else:
        print("[WARN] DDNS is disabled; peers must reach a bare IP (see ddns.*).", flush=True)

    print(
        "  Whether the router forwards the port cannot be verified from this host. "
        "Run 'nodo nat-guide' for the steps, then test from outside your network.",
        flush=True
    )


def doctor_command(main_dir):
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    service_name = "nodo.service"
    service_file_path = f"/etc/systemd/system/{service_name}"
    template_file = os.path.join(main_dir, "bash", "nodo.service.template")

    if not os.path.exists(template_file):
        print(f"Error: Template file {template_file} not found.", flush=True)
        return

    with open(template_file, "r") as f:
        try:
            expected_content = _render_service_template(f.read(), main_dir)
        except ValueError as e:
            print(f"Error: {e}", flush=True)
            return
    expected_service_user = _parse_unit_user(expected_content)

    needs_fix = False

    if not os.path.exists(service_file_path):
        print(f"Service file {service_file_path} does not exist.", flush=True)
        needs_fix = True
        current_service_user = expected_service_user
    else:
        with open(service_file_path, "r") as f:
            current_content = f.read()
        current_service_user = _parse_unit_user(current_content)

        if current_content.strip() != expected_content.strip():
            print("Service file content differs from expected configuration.", flush=True)
            needs_fix = True
        else:
            print(f"[OK] {service_name} is correctly configured.", flush=True)

    if needs_fix:
        print(f"Fixing {service_name}...", flush=True)

        subprocess.run(["systemctl", "stop", service_name], capture_output=True)
        subprocess.run(["systemctl", "disable", service_name], capture_output=True)

        with open(service_file_path, "w") as f:
            f.write(expected_content)

        os.chmod(service_file_path, 0o644)

        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "enable", service_name], capture_output=True)

        print(f"[OK] {service_name} has been fixed and enabled.", flush=True)
        print("  Run 'nodo daemon start' to start the service.", flush=True)
        current_service_user = expected_service_user

    print(f"Service runtime user for checks: {current_service_user}", flush=True)
    _doctor_kvm_checks(current_service_user)
    _doctor_cloud_hypervisor(main_dir)
    _doctor_network_checks()
