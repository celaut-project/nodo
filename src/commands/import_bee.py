import os
from protos import celaut_pb2
from grpcbigbuffer.client import read_from_file


def import_bee(path: str):
    if not os.path.exists(path):
        print(f"{path} not exists.")
        return
    
    try:
        it = read_from_file(path=path, indices={
                1: celaut_pb2.Any.Metadata,
                2: celaut_pb2.Service,
            })
            
        metadata = celaut_pb2.Any.Metadata()
        metadata.ParseFromString(open(next(it).dir, "rb").read())
        print(f"metaadat -> {metadata}")
        
        service_dir = next(it).dir
        print(f"service dir -> {service_dir}")
        
        print("Service imported correctly")
    
    except Exception as e:
        print(f"Error importing service: {e}")