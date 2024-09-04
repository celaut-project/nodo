import hashlib
import os, subprocess
from dotenv import load_dotenv, set_key
from typing import Final, Dict, Callable, Tuple
import docker as docker_lib
from protos import celaut_pb2

def _(env, default) -> Tuple[str, str]:
    """
    Fetches an environment variable value or returns the default.
    """
    GET_ENV = lambda env, default: (type(default)(os.environ.get(env)) if type(default) != bool
                                    else os.environ.get(env) in ['True', 'true', 'T', 't'])\
                                        if env in os.environ.keys() else default
    try:
        _value = GET_ENV(env=env, default=default)
    except:
        _value = default
    globals()[env] = _value

    # If the value is a string containing an expression, evaluate it.
    # First, we need to check if it contains any other global variables
    if isinstance(_value, str) and '{' in _value and '}' in _value:
        globals()[env] = _value.format(**globals())

    return env, globals()[env]

def write_env(key, value):
    globals()[key] = value
    write_default_to_file(globals())
    load_dotenv(".env")
    _(key, value)


if os.path.exists(".env"):
    load_dotenv(".env")

# ------------------------------
# ----------- START ------------
# ------------------------------

# Directory Settings
_("MAIN_DIR", "/nodo")
_("STORAGE", f"{MAIN_DIR}/storage")
_("CACHE", f"{STORAGE}/__cache__/")
_("REGISTRY", f"{STORAGE}/__registry__/")
_("METADATA_REGISTRY", f"{STORAGE}/__metadata__/")
_("BLOCKDIR", f"{STORAGE}/__block__/")
_("DATABASE_FILE", f'{STORAGE}/database.sqlite')

# Compiler Settings
_("SAVE_ALL", False)  # Save the services that compiles.
_("COMPILER_MEMORY_SIZE_FACTOR", 2.0)
_("ARM_COMPILER_SUPPORT", True)
_("X86_COMPILER_SUPPORT", False)
COMPILER_SUPPORTED_ARCHITECTURES = [
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if ARM_COMPILER_SUPPORT else [],
    ['linux/amd64', 'x86_64', 'amd64'] if X86_COMPILER_SUPPORT else []
]

# Builder Settings
_("WAIT_FOR_CONTAINER", 60)
_("BUILD_CONTAINER_MEMORY_SIZE_FACTOR", 3.1)
_("ARM_SUPPORT", True)
_("X86_SUPPORT", False)
SUPPORTED_ARCHITECTURES = [
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if ARM_SUPPORT else [],
    ['linux/amd64', 'x86_64', 'amd64'] if X86_SUPPORT else []
]

# Docker Configuration
DOCKER_COMMAND = subprocess.check_output(["which", "docker"]).strip().decode("utf-8")
_("DOCKER_CLIENT_TIMEOUT", 480)
_("DOCKER_MAX_CONNECTIONS", 1000)
DOCKER_CLIENT = lambda: docker_lib.from_env(
    timeout=DOCKER_CLIENT_TIMEOUT,
    max_pool_size=DOCKER_MAX_CONNECTIONS
)
_("CONCURRENT_CONTAINER_CREATIONS", 10)
_("REMOVE_CONTAINERS", True)
_("IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER", True)

# Network and Port Settings
_("GATEWAY_PORT", 8090)
_("NGROK_TUNNELS_KEY", "")
DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'

# Ledger
ERGO_ENVS = {env: value for env, value in {
    _("ERGO_NODE_URL", "http://213.239.193.208:9052/"),
    _("ERGO_WALLET_MNEMONIC", "decline reward asthma enter three clean borrow repeat identify wisdom horn pull entire adapt neglect"),
    _("LEDGER_SUBMISSION_THRESHOLD", 10),
    _("TOTAL_REPUTATION_TOKEN_AMOUNT", 1_000_000_000),
    _("REVIEWER_REPUTATION_PROOF_ID", "")
}}

# Logging and Memory Settings
_("MEMORY_LOGS", False)
_("MEMORY_LIMIT_COST_FACTOR", 1 / pow(10, 6))

# Cost and Deposit Settings
_("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR", 1 / pow(10, 6))
_("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR", False)
_("DEFAULT_INTIAL_GAS_AMOUNT", pow(10, 9))
_("MIN_DEPOSIT_PEER", pow(10, 64))
_("INITIAL_PEER_DEPOSIT_FACTOR", 0.5)
_("COST_AVERAGE_VARIATION", 1)
_("GAS_COST_FACTOR", 1)
_("COST_OF_BUILD", 5)
_("EXECUTION_BENEFIT", 1)
_("MODIFY_SERVICE_SYSTEM_RESOURCES_COST", 1)
_("ALLOW_GAS_DEBT", False)

# Timing and Delay Settings
_("GENERAL_WAIT_TIME", 2)
_("GENERAL_ATTEMPTS", 10)
_("MANAGER_ITERATION_TIME", 10)
_("TIME_TO_PRUNE_ZERO_CLIENT", 540)
_("COMMUNICATION_ATTEMPTS", 1)
_("COMMUNICATION_ATTEMPTS_DELAY", 60)
_("CLIENT_EXPIRATION_TIME", 1200)
_("EXTERNAL_COST_TIMEOUT", 10)

# Communication Settings
_("SEND_ONLY_HASHES_ASKING_COST", True)
_("DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH", False)

# Client Settings
_("MIN_SLOTS_OPEN_PER_PEER", 1)
_("CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME", pow(10, 3))

# Miscellaneous
_("COMPUTE_POWER_RATE", 2)
_("MIN_BUFFER_BLOCK_SIZE", 10 ** 7)
_("WEIGHT_CONFIGURATION_FACTOR", int(pow(10, 9)))
_("SOCIALIZATION_FACTOR", 2)
_("INIT_COST_CONFIGURATION_FACTOR", 1)
_("MAINTENANCE_COST_CONFIGURATION_FACTOR", pow(10, 6))
_("MEMSWAP_FACTOR", 0)
_("USE_PRINT", False)

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
    from src.utils.write_envs import write_default_to_file
    write_default_to_file(globals())
