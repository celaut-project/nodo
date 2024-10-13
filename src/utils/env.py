import hashlib
import os
import subprocess
from dotenv import load_dotenv
from typing import Final, Dict, Callable
import docker as docker_lib
from mnemonic import Mnemonic
from protos import celaut_pb2
from src.utils.singleton import Singleton
from src.utils.network import get_free_port

class EnvManager(metaclass=Singleton):
    def __init__(self):
        """
        Initialize the EnvManager by loading the .env file and setting up globals.
        """
        dotenv_path = ".env"
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path)

        self.dotenv_path = dotenv_path
        self.env_vars = {}

    def get_env(self, env: str, default = None) -> str:
        """
        Fetches an environment variable value or returns the default.
        If the value is not found, sets it with the default (if provided).
        """
        # 1. Check if the variable is already in env_vars
        if env in self.env_vars:
            return self.env_vars[env]

        # 2. Check if the variable is in .env or OS environment
        value = os.getenv(env, default=default)

        if value is None:
            # 3. If not found, use the default if provided, else return None or empty string
            if default is not None:
                value = default
                self.write_env(env, default)
            else:
                return ""

        # 4. Attempt to determine the type of the value automatically (bool, int, float, or leave as string)
        value = self._auto_cast_value(value) if type(value) is str else value

        # 5. Store the value in env_vars for future calls
        self.env_vars[env] = value
        return value

    def _auto_cast_value(self, value: str):
        """
        Attempts to automatically cast the string value to the appropriate type.
        Handles booleans, integers, floats, and defaults to string if no match.
        """
        # Handle booleans
        if value.lower() in ['true', 't']:
            return True
        elif value.lower() in ['false', 'f']:
            return False

        # Handle integers and floats
        try:
            if 'e' in value.lower() or '.' in value:
                return float(value)  # Try to cast to float
            else:
                return int(value, base=10)  # Try to cast to int
        except ValueError:
            pass  # If casting fails, just return the string

        # Return as string if no other type matched
        return value

    def write_env(self, key: str, value):
        """
        Updates the environment variable both in memory and in the .env file.
        """
        self.env_vars[key] = value
        self.write_default_to_file()
        load_dotenv(".env")  # Reload after update
        self.get_env(key, value)  # Ensure consistency

    def write_default_to_file(self):
        """
        Writes the current environment variables to the .env file.
        """
        env_file_path = os.path.join(self.env_vars['MAIN_DIR'], ".env")

        exclude_vars = {
            "get_env", "COMPILER_SUPPORTED_ARCHITECTURES", "SUPPORTED_ARCHITECTURES",
            "SHAKE_256_ID", "SHA3_256_ID", "SHAKE_256", "SHA3_256", "HASH_FUNCTIONS",
            "DOCKER_CLIENT", "DEFAULT_SYSTEM_RESOURCES", "DOCKER_COMMAND",
            "STORAGE", "CACHE", "REGISTRY", "METADATA_REGISTRY", "BLOCKDIR",
            "DATABASE_FILE", "REPUTATION_DB"
        }

        constants = {k: v for k, v in self.env_vars.items() if k.isupper() and k not in exclude_vars}

        with open(env_file_path, "w") as f:
            for key, value in constants.items():
                if isinstance(value, bool):
                    value = "True" if value else "False"
                f.write(f"{key}={value}\n")


# Instantiate EnvManager
env_manager = EnvManager()

# ------------------------------
# ----------- START ------------
# ------------------------------

# Directory Settings
env_manager.get_env("MAIN_DIR", "/nodo")
env_manager.get_env("STORAGE", f"{env_manager.env_vars['MAIN_DIR']}/storage")
env_manager.get_env("CACHE", f"{env_manager.env_vars['STORAGE']}/__cache__/")
env_manager.get_env("REGISTRY", f"{env_manager.env_vars['STORAGE']}/__registry__/")
env_manager.get_env("METADATA_REGISTRY", f"{env_manager.env_vars['STORAGE']}/__metadata__/")
env_manager.get_env("BLOCKDIR", f"{env_manager.env_vars['STORAGE']}/__block__/")
env_manager.get_env("DATABASE_FILE", f'{env_manager.env_vars["STORAGE"]}/database.sqlite')

# Compiler Settings
env_manager.get_env("SAVE_ALL", False)
env_manager.get_env("COMPILER_MEMORY_SIZE_FACTOR", 2.0)
env_manager.get_env("ARM_COMPILER_SUPPORT", True)
env_manager.get_env("X86_COMPILER_SUPPORT", False)
COMPILER_SUPPORTED_ARCHITECTURES = [
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if env_manager.env_vars["ARM_COMPILER_SUPPORT"] else [],
    ['linux/amd64', 'x86_64', 'amd64'] if env_manager.env_vars["X86_COMPILER_SUPPORT"] else []
]

# Builder Settings
env_manager.get_env("WAIT_FOR_CONTAINER", 60)
env_manager.get_env("BUILD_CONTAINER_MEMORY_SIZE_FACTOR", 3.1)
env_manager.get_env("ARM_SUPPORT", True)
env_manager.get_env("X86_SUPPORT", False)
SUPPORTED_ARCHITECTURES = [
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if env_manager.env_vars["ARM_SUPPORT"] else [],
    ['linux/amd64', 'x86_64', 'amd64'] if env_manager.env_vars["X86_SUPPORT"] else []
]

# Docker Configuration
DOCKER_COMMAND = subprocess.check_output(["which", "docker"]).strip().decode("utf-8")
env_manager.get_env("DOCKER_CLIENT_TIMEOUT", 480)
env_manager.get_env("DOCKER_MAX_CONNECTIONS", 1000)
DOCKER_CLIENT = lambda: docker_lib.from_env(
    timeout=env_manager.env_vars["DOCKER_CLIENT_TIMEOUT"],
    max_pool_size=env_manager.env_vars["DOCKER_MAX_CONNECTIONS"]
)
env_manager.get_env("CONCURRENT_CONTAINER_CREATIONS", 10)
env_manager.get_env("REMOVE_CONTAINERS", True)
env_manager.get_env("IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER", True)

# Network and Port Settings
env_manager.get_env("GATEWAY_PORT", get_free_port())
env_manager.get_env("NGROK_TUNNELS_KEY", "")
DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'

# Ledger Settings
env_manager.get_env("ERGO_NODE_URL", "http://135.181.107.130:9053/")
# env_manager.get_env("ERGO_NODE_URL", "http://213.239.193.208:9052/")   <-- TESTNET
env_manager.get_env("ERGO_WALLET_MNEMONIC", Mnemonic("english").generate(strength=128))
env_manager.get_env("ERGO_AUXILIAR_MNEMONIC", Mnemonic("english").generate(strength=128))
env_manager.get_env("ERGO_PAYMENTS_RECIVER_WALLET", "")
env_manager.get_env("SIMULATE_PAYMENTS", False)
env_manager.get_env("ERGO_ERG_HOT_WALLET_LIMITS", 100)
env_manager.get_env("LEDGER_REPUTATION_SUBMISSION_THRESHOLD", 10)
env_manager.get_env("TOTAL_REPUTATION_TOKEN_AMOUNT", 1_000_000_000)
env_manager.get_env("PAYMENT_MANAGER_ITERATION_TIME", 86_400)
env_manager.get_env("REPUTATION_PROOF_ID", "")
env_manager.get_env("ERGO_DONATION_WALLET", "9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo")
env_manager.get_env("ERGO_DONATION_PERCENTAGE", "0.00")

# Logging and Memory Settings
env_manager.get_env("MEMORY_LOGS", False)
env_manager.get_env("MEMORY_LIMIT_COST_FACTOR", 1 / pow(10, 6))

# Cost and Deposit Settings
env_manager.get_env("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR", 1 / pow(10, 6))
env_manager.get_env("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR", False)
env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT", pow(10, 9))
env_manager.get_env("TOTAL_REFILLED_DEPOSIT", pow(10, 65))
env_manager.get_env("MIN_DEPOSIT_PEER", pow(10, 64))
env_manager.get_env("FREE_TRIAL_GAS_AMOUNT",  pow(10, 66))
env_manager.get_env("INITIAL_PEER_DEPOSIT_FACTOR", 0.5)
env_manager.get_env("COST_AVERAGE_VARIATION", 1)
env_manager.get_env("GAS_COST_FACTOR", 1)
env_manager.get_env("COST_OF_BUILD", 5)
env_manager.get_env("EXECUTION_BENEFIT", 1)
env_manager.get_env("MODIFY_SERVICE_SYSTEM_RESOURCES_COST", 1)
env_manager.get_env("ALLOW_GAS_DEBT", False)

# Timing and Delay Settings
env_manager.get_env("GENERAL_WAIT_TIME", 2)
env_manager.get_env("GENERAL_ATTEMPTS", 10)
env_manager.get_env("MANAGER_ITERATION_TIME", 10)
env_manager.get_env("TIME_TO_PRUNE_ZERO_CLIENT", 540)
env_manager.get_env("COMMUNICATION_ATTEMPTS", 1)
env_manager.get_env("COMMUNICATION_ATTEMPTS_DELAY", 60)
env_manager.get_env("CLIENT_EXPIRATION_TIME", 1200)
env_manager.get_env("EXTERNAL_COST_TIMEOUT", 10)
env_manager.get_env("START_SERVICE_ON_PEER_TIMEOUT", 120)

# Communication Settings
env_manager.get_env("SEND_ONLY_HASHES_ASKING_COST", True)
env_manager.get_env("DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH", False)

# Client Settings
env_manager.get_env("MIN_SLOTS_OPEN_PER_PEER", 1)
env_manager.get_env("CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME", pow(10, 3))

# Miscellaneous Settings
env_manager.get_env("COMPUTE_POWER_RATE", 2)
env_manager.get_env("MIN_BUFFER_BLOCK_SIZE", 10 ** 7)
env_manager.get_env("WEIGHT_CONFIGURATION_FACTOR", int(pow(10, 9)))
env_manager.get_env("SOCIALIZATION_FACTOR", 2)
env_manager.get_env("INIT_COST_CONFIGURATION_FACTOR", 1)
env_manager.get_env("MAINTENANCE_COST_CONFIGURATION_FACTOR", pow(10, 6))
env_manager.get_env("MEMSWAP_FACTOR", 0)
env_manager.get_env("USE_PRINT", False)

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

env_manager.write_default_to_file()
