import os
import pwd
import grp
import re
import subprocess


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
        expected_content = f.read().replace("{{MAIN_DIR}}", main_dir)
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
