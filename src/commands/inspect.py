import os
from src.commands.__by_tag import get_id
from src.utils.env import SHA3_256_ID, SHAKE_256_ID, EnvManager
from src.utils.utils import read_metadata_from_disk, read_service_from_disk

env_manager = EnvManager()

METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
REGISTRY = env_manager.get_env("REGISTRY")


def inspect(service: str):
    service = get_id(service)

    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    print("# Metadata")
    metadata = read_metadata_from_disk(service_hash=service)

    for hash in list(metadata.hashtag.hash):
        _type = hash.type.hex()[:6]
        if SHA3_256_ID == hash.type:
            _type = f"(SHA3) {_type}"
        elif SHAKE_256_ID == hash.type:
            _type = f"(SHAKE) {_type}"

        print(f"-  {_type}: {hash.value.hex()}")

    print("Reputation proofs:")
    for contract in metadata.reputation_proofs:
        print(f"Ledger: {contract.ledger}")
        print(f"Script: {contract.contract}")  # <- This is bytes.
        print(f"Address: {contract.contract_addr}")
        print("\n")

    print("# Service")
    service_obj = read_service_from_disk(service_hash=service)

    print(f"Prose: {service_obj.prose}")
    print(f"Service Interface: {service_obj.api}")
    print("Service container:")
    print(f"    - Architecture {service.container.architecture.tags}\n{service.container.architecture.prose}")
    print(f"    - Envirment variables {service.container.enviroment_variables}")
    print(f"    - Entrypoint {service.container.entrypoint}")
    print("    - Node compatibility:")
    print(f"        - Configuration expected on: {service.container.config}")
    print(f"        - Node protocol stack: {service.container.node_protocol_stack}")
    print(f"Service network {service.network.tags}\n{service.network.prose}")