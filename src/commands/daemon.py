import hashlib
import os
import socket
import subprocess
from typing import Optional


def is_serving() -> bool:
    """Whether something answers on this node's gateway port.

    This is what decides whether a configuration change owes a restart: a file
    written while nothing is running is simply what the next start reads.
    """
    from src.utils.config import ConfigManager

    port = ConfigManager().gateway_port_or_none()
    if not port:
        return False  # No port assigned means nothing can be serving on one.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            return probe.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError as error:
        print(f"Error checking if the gateway port is in use: {error}", flush=True)
        return False


def config_digest() -> Optional[str]:
    """A fingerprint of config.yaml as it stands on disk, or None if unreadable.

    Taken before and after a command rather than trusting the command to report
    whether it wrote: what matters is catching *any* write, including one made deep
    in a library the command happens to call.
    """
    from src.utils.config import ConfigManager

    try:
        with open(ConfigManager().config_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def restart_after_config_write(before: Optional[str]) -> bool:
    """Restart a serving node when a CLI command has just written config.yaml.

    A process reads config.yaml once, so a write made from outside the daemon leaves
    the running node on the value it booted with -- and the next value that node
    persists rewrites the whole file from what it loaded, dropping the write. The
    restart is what makes the change real; it is the same step `nodo tui` performs
    for an edit made there, and the backup was already taken by whoever wrote.

    ``before`` is :func:`config_digest` taken before the command ran. Returns whether
    the running node is on the new configuration -- True as well when the file did
    not change, and when there was nothing serving to restart.
    """
    after = config_digest()
    if after is None or after == before:
        return True

    if not is_serving():
        print(
            "config.yaml was updated. No node is serving, so the next start reads it.",
            flush=True,
        )
        return True

    print("config.yaml was updated; restarting nodo so it reads the new value.", flush=True)
    if daemon_command("restart", None):
        return True

    print(
        "The change is on disk, but the running node is still on the configuration it "
        "booted with and will overwrite the change the next time it persists a value of "
        "its own. Run `sudo nodo daemon restart`.",
        flush=True,
    )
    return False


def daemon_command(subcommand, main_dir) -> bool:
    """Drive nodo.service, returning whether the command did what it says.

    The return value is what lets a caller act on the outcome instead of on the
    printed text -- notably the TUI, which applies a configuration change and its
    restart as one step and has to put the old configuration back when the restart
    does not happen. A command that prints an error and returns ``True`` would have
    it reporting a node running settings it never loaded.
    """
    _ = main_dir
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return False

    service_name = "nodo.service"

    if subcommand == "start":
        result = subprocess.run(
            ['systemctl', 'start', service_name],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"{service_name} started successfully.", flush=True)
            return True
        print(f"Failed to start {service_name}: {result.stderr}", flush=True)
        return False

    elif subcommand == "status":
        result = subprocess.run(
            ['systemctl', '--no-pager', 'status', service_name],
            capture_output=True,
            text=True
        )
        print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, flush=True)
        # Reporting the state IS what this subcommand does, and `systemctl status`
        # exits non-zero for a stopped unit. Forwarding that would make
        # `nodo daemon status` fail on a node that is merely not running.
        return True

    elif subcommand == "stop":
        result = subprocess.run(
            ['systemctl', 'stop', service_name],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"{service_name} stopped successfully.", flush=True)
            return True
        print(f"Failed to stop {service_name}: {result.stderr}", flush=True)
        return False

    elif subcommand == "restart":
        stop_result = subprocess.run(
            ['systemctl', 'stop', service_name],
            capture_output=True,
            text=True
        )
        if stop_result.returncode != 0:
            print(f"Failed to stop {service_name}: {stop_result.stderr}", flush=True)
            return False

        start_result = subprocess.run(
            ['systemctl', 'start', service_name],
            capture_output=True,
            text=True
        )
        if start_result.returncode == 0:
            print(f"{service_name} restarted successfully.", flush=True)
            return True
        print(f"Failed to start {service_name}: {start_result.stderr}", flush=True)
        return False

    else:
        print("Usage: nodo daemon <start|status|stop|restart>", flush=True)
        print("  start   - Start the nodo.service", flush=True)
        print("  status  - Show the status of nodo.service", flush=True)
        print("  stop    - Stop the nodo.service", flush=True)
        print("  restart - Restart nodo.service (stop + start)", flush=True)
        return False
