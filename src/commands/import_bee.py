import os
from protos import celaut_pb2
from grpcbigbuffer.client import read_from_file

from src.gateway.iterables.abstract_service_iterable import find_service_hash
from src.utils.env import EnvManager

env_manager = EnvManager()

REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")


def import_bee(path: str):
    if not os.path.exists(path):
        print(f"{path} not exists.")
        return
    
    try:
        it = read_from_file(path=path, indices={
                1: celaut_pb2.Any.Metadata,
                2: celaut_pb2.Service,
            })
            
        metadata_dir = next(it).dir
        metadata = celaut_pb2.Any.Metadata()
        metadata.ParseFromString(open(metadata_dir, "rb").read())
        
        service_hash = None
        for _hash in metadata.hashtag.hash:
            service_hash, service_saved = find_service_hash(_hash=_hash)
            break
                
        if not service_hash:
            print(".bee file doesn't contain service hash.  Should be implemented the task: https://github.com/celaut-project/nodo/issues/47")
            return
        
        os.system(f"mv {metadata_dir} {os.path.join(METADATA_REGISTRY, service_hash)}")
        
        service_dir = next(it).dir
        if not service_saved:
            os.system(f"mv {service_dir} {os.path.join(REGISTRY, service_hash)}")
            
        else:
            os.system(f"rm -rf {service_dir}")
        
        print("Service imported correctly")
    
    except Exception as e:
        print(f"Error importing service: {e}")
