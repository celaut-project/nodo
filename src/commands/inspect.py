import os
from src.commands.__by_tag import get_id
from src.utils.env import SHA3_256_ID, SHAKE_256_ID, EnvManager
from src.utils.utils import read_metadata_from_disk, read_service_from_disk

env_manager = EnvManager()

METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
REGISTRY = env_manager.get_env("REGISTRY")


def print_rule(title):
    width = 60
    print(f"\n{'=' * width}")
    print(f"= {title.center(width - 4)} =")
    print(f"{'=' * width}\n")


def inspect(service: str):
    service = get_id(service)

    # Metadata
    print_rule("📄 Metadata")
    metadata = read_metadata_from_disk(service_hash=service)

    # Tabla de hashes
    print(f"{'Hash type':<15} | Value")
    print(f"{'-'*15}-+-{'-'*40}")
    for h in metadata.hashtag.hash:
        _type = h.type.hex()[:6]
        if SHA3_256_ID == h.type:
            _type = f"(SHA3) {_type}"
        elif SHAKE_256_ID == h.type:
            _type = f"(SHAKE) {_type}"
        print(f"{_type:<15} | {h.value.hex()}")

    # Reputation Proofs
    print_rule("🔍 Reputation Proofs")
    for c in metadata.reputation_proofs:
        print(f"Ledger  : {c.ledger}")
        print(f"Script  : {c.contract.hex()}")
        print(f"Address : {c.contract_addr}\n")

    # Service Definition
    print_rule("🛠 Service Definition")
    service_obj = read_service_from_disk(service_hash=service)
    print(f"Prose          : {service_obj.prose}\n")
    print("Service Interface (Protobuf):")
    print(service_obj.api)
    print("\n")

    # Container Configuration
    print_rule("⚙ Container Configuration")
    print(f"Architecture : {', '.join([tag for tag in service_obj.container.architecture.tags])}")
    print(f"Descripción  : {service_obj.container.architecture.prose}")
    print(f"Env Vars     : {service_obj.container.enviroment_variables}")
    print(f"Entrypoint   : {service_obj.container.entrypoint}")
    print(f"Config File  : {service_obj.container.config}")
    print(f"Protocols    : {service_obj.container.node_protocol_stack}\n")

    # Network Settings
    print_rule("🌐 Network Settings")
    if not service_obj.network:
        print("🔒 This service is completely isolated.\n\n"
      "- It cannot connect to any external endpoints.\n"
      "- The user may connect to it through the ports exposed by its interface.\n"
      "- It can only initiate connections to its own dependencies (child services it may deploy).\n"
      "- All of its dependencies will follow the same restrictions.\n")
    for network in service_obj.network:
        print(f"Tags         : {', '.join([tag for tag in network.tags])}")
        print(f"Descripción  : {service_obj.network.prose}\n")
        print("\n")