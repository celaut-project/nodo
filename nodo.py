import sys, os, subprocess
from bee_rpc.utils import modify_env
from psutil import virtual_memory
from src.utils import logger as log
import src.manager.resources as iobd
from src.payment_system.contracts.envs import print_payment_info
from src.utils.config import ConfigManager
from src.utils.network import get_local_ip

env_manager = ConfigManager(log=log.LOGGER)

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
MEMORY_LOGS = env_manager.get("MEMORY_LOGS")
REGISTRY = env_manager.get("REGISTRY")
CACHE = env_manager.get("CACHE")
BLOCKDIR = env_manager.get("BLOCKDIR")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
DATABASE_FILE = env_manager.get("DATABASE_FILE")
MAIN_DIR = env_manager.get("MAIN_DIR")

def is_nodo_service_running():
    """Check if the nodo service is running by verifying if GATEWAY_PORT is in use."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(('localhost', int(GATEWAY_PORT)))
            return result == 0  # Port is in use (connection successful)
    except Exception as e:
        print(f"Error checking if GATEWAY_PORT is in use: {e}", flush=True)
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

def resolve_user_path(user_path: str) -> str:
    """
    Resolve a user-provided path against the original shell directory.
    """
    original_directory = os.environ.get("ORIGINAL_DIR", os.getcwd())
    if os.path.isabs(user_path):
        return user_path
    return os.path.abspath(os.path.join(original_directory, user_path))


def resolve_service_input(service_input: str) -> str:
    if ".celaut" not in service_input:
        return service_input

    from src.commands.import_bee import import_bee

    absolute_path = resolve_user_path(service_input)
    if not os.path.exists(absolute_path):
        raise FileNotFoundError(f"The file {absolute_path} does not exist")

    return import_bee(path=absolute_path)

if __name__ == '__main__':

    if not os.path.exists(os.path.join(MAIN_DIR, "storage", ".acceptedkya")):
        os.system(f"/bin/bash {MAIN_DIR}/bash/accept_kya.sh {MAIN_DIR}")

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
        log=log.LOGGER if MEMORY_LOGS else lambda message: None
    )

    modify_env(
        cache_dir=CACHE,
        mem_manager=iobd.mem_manager,
        block_dir=BLOCKDIR,
        block_depth=1
    )

    if len(sys.argv) == 1:
        print("Command needed: "
            "\n- execute <service id> | <service tag> | <'.celaut' file path>"
            "\n- estimate <service id> | <service tag> | <'.celaut' file path>"
            "\n- inspect <service id> | <service tag>"
            "\n- remove <service id> | <service tag>"
            "\n- kill <instance id>"
            "\n- increase_gas <instance id> <gas to add>"
            "\n- decrease_gas <instance id> <gas to retire>"
            "\n- services"
            "\n- tag <service id|tag> <new tag>"
            "\n- clients"
            "\n- peers"
            "\n- instances"
            "\n- instances --grouped"
            "\n- connect <ip:url>"
            "\n- disconnect <peer_id>"
            "\n- pack <project directory>"
            "\n- config"
            "\n- envs"
            "\n- tui"
            "\n- info"
            "\n- logs"
            "\n- export <service> <path>"
            "\n- export <service> <path> --raw"
            "\n- import <path>"
            "\n- publish <service id|service tag>"
            "\n- download <manifest url> [-o <output dir>]"

            "\n\n Development commands:"
            "\n- update"
            "\n- serve"
            "\n- migrate"
            "\n- storage:prune_blocks"
            "\n- test <test name>"
            "\n- ggconf <repository path>"
            "\n- submit_reputation"
            "\n- validate_reputation_proof_ownership"
            "\n- refresh_ergo_nodes"
            "\n- prune_containers"
            "\n- refresh_clients"
            "\n- tx_history"
            "\n- increase_peer_deposit <peer id> <gas to add>"
            "\n- docker <docker args>  (runs docker commands in nodo's isolated context)"
            "\n- daemon start|status|stop|restart  (control the nodo.service systemd unit)"
            "\n- doctor  (check/fix nodo.service, KVM readiness, and Cloud Hypervisor compatibility)"
            "\n\n",
              flush=True)
        try:
            if not is_nodo_service_running():
                print("\nNote: Nodo service is not running.", flush=True)
        except Exception as e:
            print(f"Error checking nodo.service status: {e}", flush=True)

    else:
        match sys.argv[1]:

            case "info":
                
                try:
                    status = "running" if is_nodo_service_running() else "not running"
                    print(f"Nodo service is currently {status}.", flush=True)
                except Exception as e:
                    print(f"Error checking nodo.service status: {e}", flush=True)

                print(f"Nodo version: {get_git_commit()}", flush=True)

                print(f"Nodo address: {get_local_ip()}:{GATEWAY_PORT}", flush=True)

                reputation_proof_id = env_manager.get('REPUTATION_PROOF_ID')
                
                try:
                    payment_info = print_payment_info()
                except Exception as e:
                    log.LOGGER(f"Error getting payment info and reputation proof {e}.")
                    payment_info = "N/A"
                
                print(f"Reputation Proof ID: {reputation_proof_id or 'N/A'} \n{payment_info}", flush=True)
                
                # dev_client = SQLConnection().get_dev_clients()[0]
                # print(f"Dev client for dev purposes: {dev_client}")

                os._exit(0)

            case "logs":
                os.system(f"tail -f {MAIN_DIR}/storage/app.log")

            case "export":

                if len(sys.argv) > 4 and sys.argv[4] == "--raw":

                    from src.commands.export_raw import export_raw
                    import os

                    # Get the path provided by the user
                    user_path = sys.argv[3]
                    absolute_path = resolve_user_path(user_path)

                    # Create the directory structure if it doesn't exist
                    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)

                    # Call the export_raw function
                    export_raw(service=sys.argv[2], path=absolute_path)
                
                else:
                    from src.commands.export_bee import export_bee
                    import os

                    # Get the path provided by the user
                    user_path = sys.argv[3]
                    absolute_path = resolve_user_path(user_path)

                    # Create the directory structure if it doesn't exist
                    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)

                    # Call the export_bee function
                    export_bee(service=sys.argv[2], path=absolute_path)

            case "import":
                from src.commands.import_bee import import_bee
                import os
                import sys

                # Get the path provided by the user
                user_path = sys.argv[2]
                absolute_path = resolve_user_path(user_path)

                # Check if the file exists
                if not os.path.exists(absolute_path):
                    print(f"Error: The file {absolute_path} does not exist")
                    sys.exit(1)

                # Call the import_bee function
                import_bee(path=absolute_path)

            case "publish":
                from src.commands.publish import publish_command
                import sys

                if len(sys.argv) < 3:
                    print(
                        "Usage: nodo publish <service id|service tag>",
                        flush=True,
                    )
                    sys.exit(1)

                service_ref = sys.argv[2]
                publish_command(service_ref=service_ref)

            case "download":
                from src.commands.download import download_command
                import sys

                if len(sys.argv) < 3:
                    print("Usage: nodo download <manifest url> [-o <output dir>]", flush=True)
                    sys.exit(1)

                manifest_url = sys.argv[2]
                output_dir = None
                if "-o" in sys.argv:
                    try:
                        output_dir = sys.argv[sys.argv.index("-o") + 1]
                    except IndexError:
                        print("Error: -o requires a value", flush=True)
                        sys.exit(1)

                download_command(url=manifest_url, output_dir=output_dir)
                
            case "execute":
                from src.commands.execute import execute
                import sys

                try:
                    arg = resolve_service_input(sys.argv[2])
                except FileNotFoundError as e:
                    print(f"Error: {str(e)}")
                    sys.exit(1)

                execute(service=arg)

            case "estimate":
                from src.commands.estimate import estimate
                import sys

                try:
                    arg = resolve_service_input(sys.argv[2])
                except FileNotFoundError as e:
                    print(f"Error: {str(e)}")
                    sys.exit(1)

                estimate(service=arg)
                
            case "update":
                if os.geteuid() != 0:
                    print("This script requires superuser privileges. Please run with sudo.")
                else:
                    os.system(f"/bin/bash {MAIN_DIR}/bash/restore_source.sh {MAIN_DIR}")
                    os.system(f"/bin/bash {MAIN_DIR}/install.sh")

            case "kill":
                from src.commands.kill import kill
                kill(instance=sys.argv[2])
                
            case "increase_gas":
                from src.commands.modify_gas import modify_gas
                modify_gas(instance=sys.argv[2], gas=int(sys.argv[3]), decrement=False)
                
            case "decrease_gas":
                from src.commands.modify_gas import modify_gas
                modify_gas(instance=sys.argv[2], gas=int(sys.argv[3]), decrement=True)

            case "remove":
                from src.commands.remove import remove
                remove(service=sys.argv[2])

            case "inspect":
                from src.commands.inspect import inspect
                inspect(service=sys.argv[2])

            case "services":
                from src.commands.services import list_services
                list_services()
            
            case "tag":
                from src.commands.services import modify_tag
                tag = sys.argv[3] if len(sys.argv) == 4 else ""
                modify_tag(service=sys.argv[2], tag=tag)
                
            case 'clients':
                from src.commands.clients import list_clients
                list_clients()
                
            case "peers":
                from src.commands.peers import list_peers
                list_peers()

            case "instances":
                from src.commands.instances import list_instances
                args = sys.argv[2:]
                groupable = "--grouped" in args and not args.remove("--grouped")
                search = " ".join(args)
                list_instances(groupable=groupable, search=search)

            case 'connect':
                from src.commands.connect import connect
                connect(sys.argv[2])

            case 'disconnect':
                from src.commands.disconnect import disconnect
                disconnect(sys.argv[2])

            case 'submit_reputation':
                from src.reputation_system.interface import submit_reputation
                result: bool = submit_reputation(force_submit=True)
                if result:
                    print("Reputation proof submitted successfully.", flush=True)
                else:
                    print("Failed to submit reputation proof.", flush=True)

            case 'validate_reputation_proof_ownership':
                from src.reputation_system.contracts.ergo.proof_validation import validate_reputation_proof_ownership
                is_valid = validate_reputation_proof_ownership()
                if is_valid:
                    print("Reputation proof ownership is valid.", flush=True)
                else:
                    print("Reputation proof ownership is invalid. Please check your environment variables.", flush=True)
                
            case 'refresh_ergo_nodes':
                from src.manager.ergo import get_refresh_peers
                get_refresh_peers()

            case 'serve':
                if not is_nodo_service_running():
                    from src.serve import serve
                    serve()
                else:
                    print("Nodo service is already running in the background. Cannot start serve.", flush=True)

            case 'config':
                from src.reputation_system.contracts.ergo.proof_validation import validate_reputation_proof_ownership
                os.system("/bin/bash bash/reconfig.sh")
                if env_manager.get("REPUTATION_PROOF_ID") and not validate_reputation_proof_ownership():
                    _msg = "The reputation proof is not associated with the provided main address. It will be removed from the node environment registry."
                    log.LOGGER(_msg)
                    print(_msg)
                    env_manager.set("REPUTATION_PROOF_ID", "")

            case 'envs':
                os.system(f"yq . {MAIN_DIR}/config.yaml")

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

            case 'pack':
                from src.commands.packer.zip_with_dockerfile.pack import pack

                import os
                import sys

                # Get the path provided by the user
                user_path = sys.argv[2]

                if "http" not in user_path[:4]:
                    absolute_path = resolve_user_path(user_path)

                    # Check if the directory exists
                    if not os.path.exists(absolute_path):
                        print(f"Error: The directory {absolute_path} does not exist")
                        sys.exit(1)

                else:
                    absolute_path = user_path  # In case it's an external git repository

                pack(directory=absolute_path)

            case "tui":
                check_rust_installation()
                os.system(f"cd {MAIN_DIR}/src/commands/tui && cargo run")

            case "ggconf":
                from src.commands.ggconf import generate_gateway_config_dev
                import os
                import sys

                absolute_path = resolve_user_path(sys.argv[2])
                if not os.path.isdir(absolute_path):
                    print(f"Error: The directory {absolute_path} does not exist")
                    sys.exit(1)

                generate_gateway_config_dev(path=absolute_path)
                
            case "prune_containers":
                # Check if script is run as root
                if os.geteuid() != 0:
                    print("This script requires superuser privileges. Please run with sudo.")
                    exit()
                
                from src.manager.maintain import maintain_vmachines
                maintain_vmachines(debug_mode=True)
                
            case "refresh_clients":
                from src.manager.maintain import maintain_clients, peer_deposits
                maintain_clients(debug_mode=True)
                peer_deposits(debug_mode=True)

            case "tx_history":
                from src.commands.tx_history import tx_history
                tx_history()

            case "increase_peer_deposit":
                from src.commands.increase_peer_deposit import increase_peer_deposit
                increase_peer_deposit(peer_id=sys.argv[2], gas=int(sys.argv[3]))

            case "docker":
                # Check if script is run as root
                if os.geteuid() != 0:
                    print("This script requires superuser privileges. Please run with sudo.")
                    exit()
                
                # Execute docker commands in nodo's isolated Docker context
                from src.utils.runtime import DOCKER_COMMAND, DOCKER_ENV
                docker_args = sys.argv[2:] if len(sys.argv) > 2 else []
                if not docker_args:
                    print("Usage: nodo docker <docker command>", flush=True)
                    print("Example: nodo docker ps", flush=True)
                    print("Example: nodo docker images", flush=True)
                    print(f"\nThis uses nodo's isolated Docker daemon.", flush=True)
                else:
                    result = subprocess.run(
                        DOCKER_COMMAND + docker_args,
                        env=DOCKER_ENV,
                    )
                    if result.returncode != 0:
                        sys.exit(result.returncode)

            case "daemon":
                from src.commands.daemon import daemon_command
                subcommand = sys.argv[2] if len(sys.argv) > 2 else None
                daemon_command(subcommand=subcommand, main_dir=MAIN_DIR)

            case "doctor":
                from src.commands.doctor import doctor_command
                doctor_command(main_dir=MAIN_DIR)

            case other:
                print('Unknown command.', flush=True)
