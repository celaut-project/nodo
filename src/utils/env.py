import hashlib
import os

import docker as docker_lib

from protos import celaut_pb2

GET_ENV = lambda env, default: ( type(default)(os.environ.get(env)) if type(default)!=bool \
                                     else os.environ.get(env) in ['True', 'true', 'T', 't'] ) if env in os.environ.keys() else default

#  -------------------------------------------------
#  -------------------------------------------------
#  DOCKERFILE AND JSON   to   PROTOBUF SERVICE SPEC.
#  -------------------------------------------------
#  -------------------------------------------------

# DIRECTORIES
HYCACHE = "/node/__hycache__/"
REGISTRY = "/node/__registry__/"

SAVE_ALL = False
COMPILER_MEMORY_SIZE_FACTOR = GET_ENV(env = 'COMPILER_MEMORY_SIZE_FACTOR', default = 2.0)

COMPILER_SUPPORTED_ARCHITECTURES = [       # The first element of each list is the Docker buildx tag.
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if GET_ENV(env = 'ARM_COMPILER_SUPPORT', default=True) else [],
    ['linux/amd64', 'x86_64', 'amd64'] if GET_ENV(env = 'X86_COMPILER_SUPPORT', default=False) else []
]


WAIT_FOR_CONTAINER = GET_ENV(env = 'WAIT_FOR_CONTAINER_TIME', default = 60)
BUILD_CONTAINER_MEMORY_SIZE_FACTOR = GET_ENV(env = 'BUILD_CONTAINER_MEMORY_SIZE_FACTOR', default = 3.1)

SUPPORTED_ARCHITECTURES = [       # The first element of each list is the Docker buildx tag.
    ['linux/arm64', 'arm64', 'arm_64', 'aarch64'] if GET_ENV(env = 'ARM_SUPPORT', default=True) else [],
    ['linux/amd64', 'x86_64', 'amd64'] if GET_ENV(env = 'X86_SUPPORT', default=False) else []
]


# Gateway

DOCKER_CLIENT = lambda: docker_lib.from_env(
    timeout=GET_ENV(env='DOCKER_CLIENT_TIMEOUT', default=480),
    max_pool_size=GET_ENV(env='DOCKER_MAX_CONNECTIONS', default=1000)
)
GATEWAY_PORT = GET_ENV(env='GATEWAY_PORT', default=8090)
MEMORY_LOGS = GET_ENV(env='MEMORY_LOGS', default=False)
IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER = GET_ENV(env='IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER', default=True)
SEND_ONLY_HASHES_ASKING_COST = GET_ENV(env='SEND_ONLY_HASHES_ASKING_COST', default=False)
DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH = GET_ENV(env='DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH', default=False)
GENERAL_WAIT_TIME = GET_ENV(env='GENERAL_WAIT_TIME', default=2)
GENERAL_ATTEMPTS = GET_ENV(env='GENERAL_ATTEMPTS', default=10)
CONCURRENT_CONTAINER_CREATIONS = GET_ENV(env='CONCURRENT_CONTAINER_CREATIONS', default=10)

# Manager

DEFAULT_SYSTEM_RESOURCES = celaut_pb2.Sysresources(
    mem_limit=50 * pow(10, 6),
)

DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'

DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = GET_ENV(env='DEFAULT_INITIAL_GAS_AMOUNT_FACTOR',
                                              default=1 / pow(10, 6))  # Percentage of the parent's gas amount.
USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = GET_ENV(env='USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR',
                                                  default=False)  # Use DEFAULT_INITIAL_GAS_AMOUNT_FACTOR to calculate the initial gas amount.
DEFAULT_INTIAL_GAS_AMOUNT = GET_ENV(env='DEFAULT_INTIAL_GAS_AMOUNT', default=pow(10, 9))
COMPUTE_POWER_RATE = GET_ENV(env='COMPUTE_POWER_RATE', default=2)
COST_OF_BUILD = GET_ENV(env='COST_OF_BUILD', default=5)
EXECUTION_BENEFIT = GET_ENV(env='EXECUTION_BENEFIT', default=1)
MANAGER_ITERATION_TIME = GET_ENV(env='MANAGER_ITERATION_TIME', default=10)
TIME_TO_PRUNE_ZERO_CLIENT = GET_ENV(env='TIME_TO_PRUNE_ZERO_CLIENT', default=540)
MEMORY_LIMIT_COST_FACTOR = GET_ENV(env='MEMORY_LIMIT_COST_FACTOR', default=1 / pow(10, 6))
MIN_DEPOSIT_PEER = GET_ENV(env='MIN_PEER_DEPOSIT', default=pow(10, 64))
INITIAL_PEER_DEPOSIT_FACTOR = GET_ENV(env='INITIAL_PEER_DEPOSIT_FACTOR', default=0.5)
COST_AVERAGE_VARIATION = GET_ENV(env='COST_AVERAGE_VARIATION', default=1)
GAS_COST_FACTOR = GET_ENV(env='GAS_COST_FACTOR',
                            default=1)  # Applied only outside the manager. (not in maintain_cost)
MODIFY_SERVICE_SYSTEM_RESOURCES_COST = GET_ENV(env='MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR', default=1)
ALLOW_GAS_DEBT = GET_ENV(env='ALLOW_GAS_DEBT', default=False)  # Could be used with the reputation system.
COMMUNICATION_ATTEMPTS = GET_ENV(env='COMMUNICATION_ATTEMPTS', default=1)
COMMUNICATION_ATTEMPTS_DELAY = GET_ENV(env='COMMUNICATION_ATTEMPTS_DELAY', default=60)
MIN_SLOTS_OPEN_PER_PEER = GET_ENV(env='MIN_SLOTS_OPEN_PER_PEER', default=1)
CLIENT_EXPIRATION_TIME = GET_ENV(env='CLIENT_EXPIRATION_TIME', default=1200)
CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME = GET_ENV(env='CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME',
                                                           default=pow(10, 3))

MEMSWAP_FACTOR = 0  # 0 - 1


# Hashes

# -- HASH IDs --
SHAKE_256_ID = bytes.fromhex("46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f")
SHA3_256_ID = bytes.fromhex("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a")

# -- HASH FUNCTIONS --
SHAKE_256 = lambda value: "" if value is None else hashlib.shake_256(value).digest(32)
SHA3_256 = lambda value: "" if value is None else hashlib.sha3_256(value).digest()

HASH_FUNCTIONS = {
    SHA3_256_ID: SHA3_256,
    SHAKE_256_ID: SHAKE_256
}