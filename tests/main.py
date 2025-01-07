import os.path
from typing import Optional

from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from src.utils.env import SHA3_256_ID, EnvManager
from src.utils.utils import to_gas_amount

env_manager = EnvManager()

METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
REGISTRY = env_manager.get_env("REGISTRY")
GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")

# Read the .services file and populate the constants dynamically
with open('tests/.services', 'r') as file:
    for line in file:
        # Split each line into variable and value (assuming they are separated by '=')
        parts = line.strip().split('=')
        if len(parts) == 2:
            variable, value = parts
            # Create constants dynamically using globals()
            globals()[variable] = value

from protos import celaut_pb2, gateway_pb2
from grpcbigbuffer.client import Dir


GATEWAY: str = next(
        f"{ip}:{port}"
        for peer_id in get_peer_ids()
        for ip, port in get_peer_directions(peer_id=peer_id)
    ) or f"localhost:{GATEWAY_PORT}"

SHA3_256 = SHA3_256_ID.hex()


def generator(_hash: str, mem_limit: int = 50 * pow(10, 6), initial_gas_amount: Optional[int] = None):
    try:
        yield gateway_pb2.Client(client_id='dev')

        yield gateway_pb2.Configuration(
            config=celaut_pb2.Configuration(),
            resources=gateway_pb2.CombinationResources(
                clause={
                    1: gateway_pb2.CombinationResources.Clause(
                        cost_weight=1,
                        min_sysreq=celaut_pb2.Sysresources(
                                mem_limit=mem_limit
                            )
                    )
                }
            ),
            initial_gas_amount=to_gas_amount(initial_gas_amount) if initial_gas_amount else None
        )

        yield celaut_pb2.Any.Metadata.HashTag.Hash(
                type=bytes.fromhex(SHA3_256),
                value=bytes.fromhex(_hash)
            )

        yield Dir(
            dir=os.path.join(METADATA_REGISTRY, _hash),
            _type=celaut_pb2.Any.Metadata
        )

        yield Dir(
            dir=os.path.join(REGISTRY, _hash),
            _type=celaut_pb2.Service
        )

    except Exception as e:
        print(f"Exception on tests: {e}")
