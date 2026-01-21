import os
import subprocess
import sys

def daemon_command(subcommand, main_dir):
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    service_name = "nodo.service"
    service_file_path = f"/etc/systemd/system/{service_name}"
    template_file = os.path.join(main_dir, "bash", "nodo.service.template")

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
    
    elif subcommand == "doctor":
        # Read template and replace placeholder
        if not os.path.exists(template_file):
            print(f"Error: Template file {template_file} not found.", flush=True)
            return
        
        with open(template_file, 'r') as f:
            expected_content = f.read().replace("{{MAIN_DIR}}", main_dir)
        
        needs_fix = False
        
        # Check if service file exists
        if not os.path.exists(service_file_path):
            print(f"Service file {service_file_path} does not exist.", flush=True)
            needs_fix = True
        else:
            # Read current service file content
            with open(service_file_path, 'r') as f:
                current_content = f.read()
            
            if current_content.strip() != expected_content.strip():
                print("Service file content differs from expected configuration.", flush=True)
                needs_fix = True
            else:
                print(f"✓ {service_name} is correctly configured.", flush=True)
        
        if needs_fix:
            print(f"Fixing {service_name}...", flush=True)
            
            # Stop and disable service if it exists
            subprocess.run(['systemctl', 'stop', service_name], capture_output=True)
            subprocess.run(['systemctl', 'disable', service_name], capture_output=True)
            
            # Write the correct service file
            with open(service_file_path, 'w') as f:
                f.write(expected_content)
            
            # Set correct permissions
            os.chmod(service_file_path, 0o644)
            
            # Reload systemd and enable service
            subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
            subprocess.run(['systemctl', 'enable', service_name], capture_output=True)
            
            print(f"✓ {service_name} has been fixed and enabled.", flush=True)
            print("  Run 'nodo daemon start' to start the service.", flush=True)
    
    else:
        print("Usage: nodo daemon <start|status|stop|doctor>", flush=True)
        print("  start   - Start the nodo.service", flush=True)
        print("  status  - Show the status of nodo.service", flush=True)
        print("  stop    - Stop the nodo.service", flush=True)
        print("  doctor  - Check and fix nodo.service if needed", flush=True)
