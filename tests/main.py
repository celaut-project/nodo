import os.path
from typing import Optional

from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from src.utils.hashing import SHA3_256_ID
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
GATEWAY_PORT = env_manager.get("GATEWAY_PORT")

# Read the .services file and populate the constants dynamically
with open('tests/.services', 'r') as file:
    for line in file:
        # Split each line into variable and value (assuming they are separated by '=')
        parts = line.strip().split('=')
        if len(parts) == 2:
            variable, value = parts
            # Create constants dynamically using globals()
            globals()[variable] = value

from protos import celaut_pb2, celaut_pb2
from bee_rpc.client import Dir


GATEWAY: str = next(
        f"{ip}:{port}"
        for peer_id in get_peer_ids()
        for ip, port, _transport in get_peer_directions(peer_id=peer_id)
    ) or f"localhost:{GATEWAY_PORT}"

SHA3_256 = SHA3_256_ID.hex()


def generator(_hash: str, mem_limit: int = 50 * pow(10, 6), initial_gas_amount: Optional[int] = None):
    try:
        yield celaut_pb2.Client(client_id='dev')

        yield celaut_pb2.Configuration(
            initial_gas_amount=to_gas_amount(initial_gas_amount) if initial_gas_amount else None
        )

        yield celaut_pb2.Metadata.HashTag.Hash(
                type=bytes.fromhex(SHA3_256),
                value=bytes.fromhex(_hash)
            )

        yield Dir(
            dir=os.path.join(METADATA_REGISTRY, _hash),
            _type=celaut_pb2.Metadata
        )

        yield Dir(
            dir=os.path.join(REGISTRY, _hash),
            _type=celaut_pb2.Service
        )

    except Exception as e:
        print(f"Exception on tests: {e}")
