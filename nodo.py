import sys, os, subprocess
from grpcbigbuffer import utils as grpcbf
from psutil import virtual_memory
from src.utils import logger as l
import src.manager.resources_manager as iobd
from src.payment_system.contracts.envs import print_payment_info
from src.utils.env import EnvManager
from src.utils.network import get_local_ip
from src.database.sql_connection import SQLConnection

env_manager = EnvManager()

GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")
MEMORY_LOGS = env_manager.get_env("MEMORY_LOGS")
REGISTRY = env_manager.get_env("REGISTRY")
CACHE = env_manager.get_env("CACHE")
BLOCKDIR = env_manager.get_env("BLOCKDIR")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
MAIN_DIR = env_manager.get_env("MAIN_DIR")

# Function to check if nodo.service is running
def is_nodo_service_running():
    try:
        # Run systemctl status nodo.service and capture output
        result = subprocess.run(['systemctl', 'status', 'nodo.service'], capture_output=True, text=True)
        # Check if systemctl command indicates that nodo.service is active (running)
        return "Active: active" in result.stdout
    except Exception as e:
        print(f"Error checking nodo.service status: {e}", flush=True)
        return False  # Return False to be safe if there's an error

def stop_service():
    try:
        # Run systemctl stop nodo.service
        result = subprocess.run(['systemctl', 'stop', 'nodo.service'], capture_output=True, text=True)
        # Check if the command was successful
        if result.returncode == 0:
            print("nodo.service stopped successfully.", flush=True)
            return True
        else:
            print(f"Failed to stop nodo.service: {result.stderr}", flush=True)
            return False
    except Exception as e:
        print(f"Error stopping nodo.service: {e}", flush=True)
        return False

def get_git_commit():
    try:
        # Get the latest commit hash
        commit_hash = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode('utf-8').strip()
        return commit_hash
    except Exception as e:
        return f"Error getting git commit: {e}"

def check_rust_installation():
    try:
        # Try to run 'rustc --version' to check if Rust is installed
        subprocess.run(['rustc', '--version'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Rust is already installed.", flush=True)
        subprocess.run(["rm", "-y", f"{MAIN_DIR}/src/commands/tui/Cargo.lock"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Installing Rust (Cargo)...", flush=True)
        try:
            # Run the command to install Rust
            subprocess.run(
                'curl --proto \'=https\' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y',
                check=True,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            print("Rust installation completed.", flush=True)

            # Load Rust environment variables directly in the current process
            cargo_bin_path = os.path.expanduser("~/.cargo/bin")
            
            # Check if $HOME/.cargo/bin exists and add it to PATH
            if os.path.exists(cargo_bin_path):
                os.environ["PATH"] += os.pathsep + cargo_bin_path
                print(f"Updated PATH with Rust binaries: {cargo_bin_path}", flush=True)
            else:
                print(f"Rust binaries directory not found: {cargo_bin_path}", flush=True)

            # Verify installation by checking rustc version again
            subprocess.run(['rustc', '--version'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print("Rust has been successfully installed and configured.", flush=True)
        except subprocess.CalledProcessError as e:
            print("Error installing Rust:", e, flush=True)

if __name__ == '__main__':
    os.umask(0o002)

    # Create __cache__ if it does not exist.
    if not os.path.exists(CACHE):
        os.makedirs(CACHE)

    # Create __registry__ if it does not exist.
    if not os.path.exists(REGISTRY):
        os.makedirs(REGISTRY)

    # Create __metadata__ if it does not exist.
    if not os.path.exists(METADATA_REGISTRY):
        os.makedirs(METADATA_REGISTRY)

    # Create __block__ if it does not exist.
    if not os.path.exists(BLOCKDIR):
        os.makedirs(BLOCKDIR)

    iobd.IOBigData(
        ram_pool_method=lambda: virtual_memory().available
    ).set_log(
        log=l.LOGGER if MEMORY_LOGS else lambda message: None
    )

    grpcbf.modify_env(
        cache_dir=CACHE,
        mem_manager=iobd.mem_manager,
        block_dir=BLOCKDIR,
        block_depth=1
    )

    if len(sys.argv) == 1:
        print("Command needed: "
              "\n- execute <service id>"
              "\n- remove <service id>"
              "\n- stop <instance id>"
              "\n- services"
              "\n- connect <ip:url>"
              "\n- compile <project directory>"
              "\n- config"
              "\n- tui"
              "\n- status"
              "\n- logs"

              "\n\n Advanced commands:"
              "\n- serve"
              "\n- migrate"
              "\n- storage:prune_blocks"
              "\n- test <test name>"
              "\n\n",
              flush=True)
        try:
            if not is_nodo_service_running():
                print("\nNote: Nodo service is not running.", flush=True)
        except Exception as e:
            print(f"Error checking nodo.service status: {e}", flush=True)

    else:
        match sys.argv[1]:

            case "status":
                try:
                    status = "running" if is_nodo_service_running() else "not running"
                    print(f"Nodo service is currently {status}.", flush=True)
                except Exception as e:
                    print(f"Error checking nodo.service status: {e}", flush=True)

                print(f"Nodo version: {get_git_commit()}", flush=True)

                print(f"Nodo address: {get_local_ip()}:{GATEWAY_PORT}", flush=True)

                payment_info = print_payment_info()
                reputation_proof_id = env_manager.get_env('REPUTATION_PROOF_ID')

                print(
                    f"Reputation Proof ID: {reputation_proof_id or 'N/A'} \n{payment_info}",
                    flush=True
                )

                dev_client = SQLConnection().get_dev_clients()[0]
                print(f"Dev client for dev purposes: {dev_client}")

            case "logs":
                os.system(f"tail -f {MAIN_DIR}/storage/app.log")

            case "execute":
                from src.commands.execute import execute
                execute(service=sys.argv[2])

            case "stop":
                from src.commands.stop import stop
                stop(instance=sys.argv[2])

            case "remove":
                from src.commands.remove import remove
                remove(service=sys.argv[2])

            case "services":
                from src.commands.services import list_services
                list_services()

            case 'connect':
                from src.utils.zeroconf import connect
                connect(sys.argv[2])

            case 'submit_reputation':
                from src.reputation_system.interface import submit_reputation
                submit_reputation()

            case 'daemon':
                from src.serve import serve
                serve()

            case 'serve':
                if not is_nodo_service_running():
                    from src.serve import serve
                    serve()
                else:
                    print("Nodo service is already running in the background. Cannot start serve.", flush=True)

            case 'config':
                os.system("chmod +x bash/reconfig.sh && ./bash/reconfig.sh")

            case 'migrate':
                import os
                from src.database.migrate import migrate
                os.system(f"rm {DATABASE_FILE}")
                migrate()

            case 'storage:prune_blocks':
                from src.commands.storage import prune_blocks
                prune_blocks()

            case 'test':
                _t = sys.argv[2]
                getattr(__import__(f"tests.{_t}", fromlist=[_t]), _t)()  # Import the test passed on param.

            case 'compile':
                from src.commands.compile.compile import compile_directory
                compile_directory(directory=sys.argv[2])

            case "tui":
                check_rust_installation()
                os.system(f"cd {MAIN_DIR}/src/commands/tui && cargo run")

            case "rundev":
                from src.commands.run_dev import run_dev
                run_dev(path=sys.argv[2])

            case other:
                print('Unknown command.', flush=True)
