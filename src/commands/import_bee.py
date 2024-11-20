import os
from grpcbigbuffer.reader import read_from_registry as bee_reader


def import_bee(path: str):
    if not os.path.exists(path):
        print(f"{path} not exists.")
        return
    
    for buff in bee_reader(filename=path):
        pass