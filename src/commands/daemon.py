import os
import subprocess

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
