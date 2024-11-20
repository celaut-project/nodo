import os
from protos import celaut_pb2
from grpcbigbuffer.client import read_from_file


def import_bee(path: str):
    if not os.path.exists(path):
        print(f"{path} not exists.")
        return
    
    try:
        read_from_file(path=path, indices={
                1: celaut_pb2.Any.Metadata,
                2: celaut_pb2.Service,
            })
        
        print("Service imported correctly")
    
    except Exception as e:
        print(f"Error importing service: {e}")