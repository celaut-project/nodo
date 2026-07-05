import os
from bee_rpc.utils import getsize
from src.commands.__by_tag import get_id
from src.utils.hashing import SHA3_256_ID, SHAKE_256_ID
from src.utils.config import ConfigManager
from src.utils.utils import read_metadata_from_disk, read_service_from_disk
from src.utils.contract_xattrs import get_address, get_script, get_token_id

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")

def format_size(bytes):
    """Convert bytes to a human-readable format (B, KB, MB, GB, TB)."""
    if bytes < 1024:
        return f"{bytes} B"
    elif bytes < 1024 ** 2:
        return f"{bytes / 1024:.2f} KB"
    elif bytes < 1024 ** 3:
        return f"{bytes / (1024 ** 2):.2f} MB"
    elif bytes < 1024 ** 4:
        return f"{bytes / (1024 ** 3):.2f} GB"
    else:
        return f"{bytes / (1024 ** 4):.2f} TB"

def print_rule(title, borders=False):
    width = 60
    if borders:
        print(f"\n{'=' * width}")
        print(f"= {title.center(width - 4)} =")
        print(f"{'=' * width}\n")
    else:
        adjusted_title = f' {title} '
        line = adjusted_title.center(width, '-')
        print(line)

def inspect(service: str):
    service = get_id(service)

    # Metadata
    print_rule("📄 Metadata", borders=True)
    metadata = read_metadata_from_disk(service_hash=service)

    # Hash table
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
        print(f"{'Ledger':<10}: {', '.join(c.ledger.tags)}")
        print(f"{'Script':<10}: {get_script(c).hex()}")
        print(f"{'Address':<10}: {get_address(c)}")
        print(f"{'Token ID':<10}: {get_token_id(c)}\n")

    # Service Definition
    print_rule("📦 Service Definition", borders=True)
    service_obj = read_service_from_disk(service_hash=service)
    service_path = os.path.join(REGISTRY, service)
    service_size = getsize(service_path)
    print(f"Size: {format_size(service_size)}")
    print(f"Prose: {service_obj.prose}\n")

    print_rule("🔌 Service Interface")
    try:
        if service_obj.container.environment_variables:
            print("Env Vars:")
            for var, df in service_obj.container.environment_variables.items():
                print(f"  - {var}: tags={df.tags}, prose='{df.prose}'")
    except Exception as e:
        print(f"Error reading environment variables: {e}")
        
    print("(Protobuf):")
    print(service_obj.api)
    print("\n")

    # Container Configuration
    print_rule("⚙ Machine Configuration")
    print(f"Architecture: {', '.join([tag for tag in service_obj.container.architecture.tags])}")
    print(f"Prose: {service_obj.container.architecture.prose}")
    print(f"Init entry_path: {list(service_obj.container.init.entry_path)}")

    if service_obj.container.resources:
        print("Resources:")
        resources = service_obj.container.resources

        def print_sysresources(label, sysres):
            if not sysres:
                return
            print(f"  {label}:")
            if sysres.blkio_weight:
                print(f"    - blkio_weight: {sysres.blkio_weight}")
            if sysres.cpu_period:
                print(f"    - cpu_period: {sysres.cpu_period}")
            if sysres.cpu_quota:
                print(f"    - cpu_quota: {sysres.cpu_quota}")
            if sysres.mem_limit:
                print(f"    - mem_limit: {format_size(sysres.mem_limit)}")
            if sysres.disk_space:
                print(f"    - disk_space: {format_size(sysres.disk_space)}")

        print_sysresources("At Init", resources.at_init)
        print_sysresources("At Most", resources.at_most)

        print("")

    print("Node compatibility")
    print(f"- Config Declaration: {service_obj.container.config_declaration}")
    print("- Protocols:")
    for proto in service_obj.container.node_protocol_stack:
        print(f"    * Tags: {proto.tags}")
        print(f"      Prose: {proto.prose}")

    # Network Settings
    print_rule("🌐 Network Settings")
    if not service_obj.network:
        print("🔒 This service is completely isolated.\n\n"
              "- It cannot connect to any external endpoints.\n"
              "- The user may connect to it through the ports exposed by its interface.\n"
              "- It can only initiate connections to its own dependencies (child services it may deploy).\n"
              "- All of its dependencies will follow the same restrictions.\n")
    for network in service_obj.network:
        print(f"Tags: {', '.join([tag for tag in network.tags])}")
        print(f"Prose: {network.prose}\n")
        print("\n")
