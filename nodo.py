import sys, os, subprocess
from grpcbigbuffer import utils as grpcbf
from psutil import virtual_memory
from src.utils import logger as l
import src.manager.resources_manager as iobd
from src.utils.env import MEMORY_LOGS, REGISTRY, CACHE, BLOCKDIR, METADATA_REGISTRY, DATABASE_FILE, MAIN_DIR

# Function to check if nodo.service is running
def is_nodo_service_running():
    try:
        # Run systemctl status nodo.service and capture output
        result = subprocess.run(['systemctl', 'status', 'nodo.service'], capture_output=True, text=True)
        # Check if systemctl command indicates that nodo.service is active (running)
        return "Active: active" in result.stdout
    except Exception as e:
        print(f"Error checking nodo.service status: {e}")
        return False  # Return False to be safe if there's an error

def stop_service():
    try:
        # Run systemctl stop nodo.service
        result = subprocess.run(['systemctl', 'stop', 'nodo.service'], capture_output=True, text=True)
        # Check if the command was successful
        if result.returncode == 0:
            print("nodo.service stopped successfully.")
            return True
        else:
            print(f"Failed to stop nodo.service: {result.stderr}")
            return False
    except Exception as e:
        print(f"Error stopping nodo.service: {e}")
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
        print("Rust is already installed.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Installing Rust (Cargo)...")
        try:
            # Run the command to install Rust
            subprocess.run(
                'curl --proto \'=https\' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y',
                check=True,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            print("Sourcing the Rust environment...")
            # Source $HOME/.cargo/env in the current environment
            os.system("source $HOME/.cargo/env")
        except subprocess.CalledProcessError as e:
            print("Error installing Rust:", e)

def run_cargo():
    try:
        # Change to the directory and run 'cargo run'
        subprocess.run(
            ['cargo', 'run'],
            check=True,
            cwd=f'{MAIN_DIR}/src/commands/tui',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        print("Error running cargo run:", e)

if __name__ == '__main__':

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
              "\n- seeder"
              "\n- connect"
              "\n- serve"
              "\n- migrate"
              "\n- containers --stream"
              "\n- peers --stream"
              "\n- prune:peer <peer_id>"
              "\n- prune:peers"
              "\n- ledgers --stream"
              "\n- view:contract"
              "\n- deploy:contract"
              "\n- storage:prune_blocks"
              "\n- test <test name>"
              "\n- compile <project directory>"
              "\n- tui"
              )
        try:
            if is_nodo_service_running():
                print("\nNote: Nodo service is currently running in the background.")
            else:
                print("\nNote: Nodo service is not running.")
        except Exception as e:
            print(f"Error checking nodo.service status: {e}")

        print(f"Nodo version (Git commit): {get_git_commit()}")
    
    else:
        match sys.argv[1]:

            case "seeder":
                from src.payment_system.contracts.ethereum.seeder import seed
                seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

            case 'connect':
                from src.utils.zeroconf import connect
                connect(sys.argv[2])

            case 'serve':
                if not is_nodo_service_running():
                    from src.serve import serve
                    serve()
                else:
                    print("Nodo service is already running in the background. Cannot start serve.")

            case 'service':
                # if is_nodo_service_running(): stop_service()
                from src.serve import serve
                serve()

            case 'migrate':
                import os
                from src.database.migrate import migrate
                from src.payment_system.contracts.ethereum.seeder import seed
                os.system(f"rm {DATABASE_FILE}")
                migrate()
                seed() if len(sys.argv) == 2 else seed(private_key=sys.argv[2])

            case 'containers':
                from src.commands.containers import containers
                containers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

            case 'peers':
                from src.commands.peers import peers
                peers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

            case 'prune:peer':
                from src.commands.peers import delete
                delete(sys.argv[2])

            case 'prune:peers':
                from src.commands.peers import delete_all
                delete_all()

            case 'ledgers':
                from src.commands.ledgers import ledgers
                ledgers(stream=len(sys.argv) == 3 and sys.argv[2] == '--stream')

            case 'view:contract':
                from src.commands.ledgers import view
                view(sys.argv[2])

            case 'deploy:contract':
                from src.payment_system.contracts.ethereum.deploy import deploy
                deploy()

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
                run_cargo()

            case other:
                print('Unknown command.')
