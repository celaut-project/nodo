import os


def proc_state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            stat = f.read()
    except FileNotFoundError:
        return ""
    except PermissionError:
        return "?"
    except Exception:
        return "?"

    # /proc/<pid>/stat has the process name in parentheses and the state right
    # after it. Split from the right because process names may contain spaces.
    try:
        return stat.rsplit(")", 1)[1].strip().split()[0]
    except Exception:
        return "?"


def pid_matches_vmachine(pid: int, vmachine_id: str) -> bool:
    expected_name = f"nodo-ch-{vmachine_id[:8]}"
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw_cmdline = f.read()
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True

    if not raw_cmdline:
        return False
    argv = [arg.decode("utf-8", errors="replace") for arg in raw_cmdline.split(b"\0") if arg]
    return bool(argv) and os.path.basename(argv[0]) == expected_name


def pid_alive(pid: int, vmachine_id: str = "") -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # If we cannot signal it, the process still exists.
        return True
    except Exception:
        return False

    if proc_state(pid) == "Z":
        return False
    if vmachine_id and not pid_matches_vmachine(pid=pid, vmachine_id=vmachine_id):
        return False
    return True
