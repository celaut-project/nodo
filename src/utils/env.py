import hashlib
import os
import subprocess
from dotenv import load_dotenv
from typing import Final, Dict, Callable, Tuple
import docker as docker_lib
from protos import celaut_pb2

def get_env(env: str, default) -> Tuple[str, str]:
    """
    Fetches an environment variable value or returns the default.
    """
    value = os.getenv(env, default)

    if isinstance(default, bool):
        value = value in ['True', 'true', 'T', 't']
    else:
        value = type(default)(value)  # type: ignore

    globals()[env] = value

    # Evaluate expressions within the string
    if isinstance(value, str) and '{' in value and '}' in value:
        globals()[env] = value.format(**globals())

    return env, globals()[env]

def write_env(key: str, value):
    """
    Updates the global environment variable and writes it to the .env file.
    """
    globals()[key] = value
    write_default_to_file(globals())
    load_dotenv(".env")
    get_env(key, value)

def write_default_to_file(global_vars: Dict[str, any]):  # type: ignore
    """
    Writes the environment variables to the .env file if it doesn't already exist.
    """
    env_file_path = os.path.join(global_vars['MAIN_DIR'], ".env")

    if not os.path.exists(env_file_path):
        exclude_vars = {
            "get_env", "COMPILER_SUPPORTED_ARCHITECTURES", "SUPPORTED_ARCHITECTURES",
            "SHAKE_256_ID", "SHA3_256_ID", "SHAKE_256", "SHA3_256", "HASH_FUNCTIONS",
            "DOCKER_CLIENT", "DEFAULT_SYSTEM_RESOURCES", "DOCKER_COMMAND",
            "STORAGE", "CACHE", "REGISTRY", "METADATA_REGISTRY", "BLOCKDIR",
            "DATABASE_FILE", "REPUTATION_DB"
        }

        constants = {k: v for k, v in global_vars.items() if k.isupper() and k not in exclude_vars}

        with open(env_file_path, "w") as f:
            for key, value in constants.items():
                if isinstance(value, bool):
                    value = "True" if value else "False"
                f.write(f"{key}={value}\n")

        print(f"Default environment variables written to {env_file_path}")
    else:
        print(f"The .env file already exists at {env_file_path}")

if os.path.exists(".env"):
    load_dotenv(".env")

# ------------------------------
# ----------- START ------------
# ------------------------------

# Directory Settings
get_env("MAIN_DIR", "/nodo")
get_env("STORAGE", f"{MAIN_DIR}/storage")  # type: ignore
get_env("CACHE", f"{STORAGE}/__cache__/")  # type: ignore
get_env("REGISTRY", f"{STORAGE}/__registry__/")  # type: ignore
get_env("METADATA_REGISTRY", f"{STORAGE}/__metadata__/")  # type: ignore
get_env("BLOCKDIR", f"{STORAGE}/__block__/")  # type: ignore
get_env("DATABASE_FILE", f'{STORAGE}/database.sqlite')  # type: ignore

# Compiler Settings
get_env("SAVE_ALL", False)
get_env("COMPILER_MEMORY_SIZE_FACTOR", 2.0)
get_env("ARM_COMPILER_SUPPORT", True)
get_env("X86_COMPILER_SUPPORT", False)
COMPILER_SUPPORTED_ARCHITECTURES = [
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if ARM_COMPILER_SUPPORT else [],  # type: ignore
    ['linux/amd64', 'x86_64', 'amd64'] if X86_COMPILER_SUPPORT else []  # type: ignore
]

# Builder Settings
get_env("WAIT_FOR_CONTAINER", 60)
get_env("BUILD_CONTAINER_MEMORY_SIZE_FACTOR", 3.1)
get_env("ARM_SUPPORT", True)
get_env("X86_SUPPORT", False)
SUPPORTED_ARCHITECTURES = [
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if ARM_SUPPORT else [],  # type: ignore
    ['linux/amd64', 'x86_64', 'amd64'] if X86_SUPPORT else []  # type: ignore
]

# Docker Configuration
DOCKER_COMMAND = subprocess.check_output(["which", "docker"]).strip().decode("utf-8")
get_env("DOCKER_CLIENT_TIMEOUT", 480)
get_env("DOCKER_MAX_CONNECTIONS", 1000)
DOCKER_CLIENT = lambda: docker_lib.from_env(
    timeout=DOCKER_CLIENT_TIMEOUT,  # type: ignore
    max_pool_size=DOCKER_MAX_CONNECTIONS # type: ignore
)
get_env("CONCURRENT_CONTAINER_CREATIONS", 10)
get_env("REMOVE_CONTAINERS", True)
get_env("IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER", True)

# Network and Port Settings
get_env("GATEWAY_PORT", 8090)
get_env("NGROK_TUNNELS_KEY", "")
DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'

# Ledger
def ERGO_ENVS(): return { env: value for env, value in [
        get_env("ERGO_NODE_URL", "http://213.239.193.208:9052/"),
        get_env("ERGO_WALLET_MNEMONIC", "decline reward asthma enter three clean borrow repeat identify wisdom horn pull entire adapt neglect"),
        get_env("LEDGER_SUBMISSION_THRESHOLD", 10),
        get_env("TOTAL_REPUTATION_TOKEN_AMOUNT", 1_000_000_000),
        get_env("REVIEWER_REPUTATION_PROOF_ID", "")
]}

# Logging and Memory Settings
get_env("MEMORY_LOGS", False)
get_env("MEMORY_LIMIT_COST_FACTOR", 1 / pow(10, 6))

# Cost and Deposit Settings
get_env("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR", 1 / pow(10, 6))
get_env("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR", False)
get_env("DEFAULT_INTIAL_GAS_AMOUNT", pow(10, 9))
get_env("MIN_DEPOSIT_PEER", pow(10, 64))
get_env("INITIAL_PEER_DEPOSIT_FACTOR", 0.5)
get_env("COST_AVERAGE_VARIATION", 1)
get_env("GAS_COST_FACTOR", 1)
get_env("COST_OF_BUILD", 5)
get_env("EXECUTION_BENEFIT", 1)
get_env("MODIFY_SERVICE_SYSTEM_RESOURCES_COST", 1)
get_env("ALLOW_GAS_DEBT", False)

# Timing and Delay Settings
get_env("GENERAL_WAIT_TIME", 2)
get_env("GENERAL_ATTEMPTS", 10)
get_env("MANAGER_ITERATION_TIME", 10)
get_env("TIME_TO_PRUNE_ZERO_CLIENT", 540)
get_env("COMMUNICATION_ATTEMPTS", 1)
get_env("COMMUNICATION_ATTEMPTS_DELAY", 60)
get_env("CLIENT_EXPIRATION_TIME", 1200)
get_env("EXTERNAL_COST_TIMEOUT", 10)

# Communication Settings
get_env("SEND_ONLY_HASHES_ASKING_COST", True)
get_env("DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH", False)

# Client Settings
get_env("MIN_SLOTS_OPEN_PER_PEER", 1)
get_env("CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME", pow(10, 3))

# Miscellaneous
get_env("COMPUTE_POWER_RATE", 2)
get_env("MIN_BUFFER_BLOCK_SIZE", 10 ** 7)
get_env("WEIGHT_CONFIGURATION_FACTOR", int(pow(10, 9)))
get_env("SOCIALIZATION_FACTOR", 2)
get_env("INIT_COST_CONFIGURATION_FACTOR", 1)
get_env("MAINTENANCE_COST_CONFIGURATION_FACTOR", pow(10, 6))
get_env("MEMSWAP_FACTOR", 0)
get_env("USE_PRINT", False)

# Hashes

# -- HASH IDs --
SHAKE_256_ID: Final[bytes] = bytes.fromhex("46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f")
SHA3_256_ID: Final[bytes] = bytes.fromhex("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a")

# -- HASH FUNCTIONS --
SHAKE_256: Callable[[bytes], bytes] = lambda value: b"" if value is None else hashlib.shake_256(value).digest(32)
SHA3_256: Callable[[bytes], bytes] = lambda value: b"" if value is None else hashlib.sha3_256(value).digest()

HASH_FUNCTIONS: Final[Dict[bytes, Callable[[bytes], bytes]]] = {
    SHA3_256_ID: SHA3_256,
    SHAKE_256_ID: SHAKE_256
}

# Default System Resources for Manager
DEFAULT_SYSTEM_RESOURCES: celaut_pb2.Sysresources = celaut_pb2.Sysresources(
    mem_limit=50 * pow(10, 6),
)

# ------------------------------
# ----------- END --------------
# ------------------------------

if not os.path.exists(".env"):
    write_default_to_file(globals())
