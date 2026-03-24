import os
import subprocess

def daemon_command(subcommand, main_dir):
    _ = main_dir
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    service_name = "nodo.service"

    if subcommand == "start":
        result = subprocess.run(
            ['systemctl', 'start', service_name],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"{service_name} started successfully.", flush=True)
        else:
            print(f"Failed to start {service_name}: {result.stderr}", flush=True)
    
    elif subcommand == "status":
        result = subprocess.run(
            ['systemctl', '--no-pager', 'status', service_name],
            capture_output=True,
            text=True
        )
        print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, flush=True)
    
    elif subcommand == "stop":
        result = subprocess.run(
            ['systemctl', 'stop', service_name],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"{service_name} stopped successfully.", flush=True)
        else:
            print(f"Failed to stop {service_name}: {result.stderr}", flush=True)

    elif subcommand == "restart":
        stop_result = subprocess.run(
            ['systemctl', 'stop', service_name],
            capture_output=True,
            text=True
        )
        if stop_result.returncode != 0:
            print(f"Failed to stop {service_name}: {stop_result.stderr}", flush=True)
            return

        start_result = subprocess.run(
            ['systemctl', 'start', service_name],
            capture_output=True,
            text=True
        )
        if start_result.returncode == 0:
            print(f"{service_name} restarted successfully.", flush=True)
        else:
            print(f"Failed to start {service_name}: {start_result.stderr}", flush=True)
    
    else:
        print("Usage: nodo daemon <start|status|stop|restart>", flush=True)
        print("  start   - Start the nodo.service", flush=True)
        print("  status  - Show the status of nodo.service", flush=True)
        print("  stop    - Stop the nodo.service", flush=True)
        print("  restart - Restart nodo.service (stop + start)", flush=True)
