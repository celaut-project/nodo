import os
from hashlib import sha3_256

from typing import Optional

import grpc
from grpcbigbuffer import client as grpcbb

from protos import celaut_pb2, compile_pb2, gateway_pb2_grpcbf, gateway_pb2_grpc
from src.commands.compile.generate_service_zip import __generate_service_zip
from src.database.access_functions.peers import get_peer_ids, get_peer_directions


def __compile(zip, node: str):
    yield from grpcbb.client_grpc(
        method=gateway_pb2_grpc.GatewayStub(
            grpc.insecure_channel(node)
        ).Compile,
        input=grpcbb.Dir(dir=zip, _type=bytes),
        indices_serializer={0: bytes},
        indices_parser=gateway_pb2_grpcbf.CompileOutput_indices,
        partitions_message_mode_parser={1: True, 2: True, 3: False}
    )


def __on_peer(peer: str, service_zip_dir: str):
    _id: Optional[str] = None
    print(f'Start compile on {peer}')
    for b in __compile(
            zip=service_zip_dir,
            node=peer
    ):
        print('b -> ', b, type(b), b.type if type(b) is grpcbb.Dir else None)
        print(f"_id -> {_id}")
        if type(b) is compile_pb2.CompileOutputServiceId:
            if _id:
                print(f"Ya se había proporcionado el id {_id}")
            _id = b.id.hex()
            print(f"El id es {_id}")
        elif type(b) == celaut_pb2.Any.Metadata and _id:
            with open(f"__metadata__/{_id}", "wb") as f:
                f.write(b.SerializeToString())
        elif type(b) == grpcbb.Dir and b.type == compile_pb2.Service and _id:
            # b is ServiceWithMeta grpc-bb cache directory.
            os.system('mv ' + b.dir + ' ' + '__registry__/' + _id)
        else:
            raise Exception('\nError with the compiler output:' + str(b))

    os.remove(service_zip_dir)
    print('service id -> ', _id)

    print('\n Validate the content.')

    validate_id = sha3_256()

    try:
        for i in grpcbb.read_multiblock_directory('__registry__/' + _id + '/'):
            validate_id.update(i)
    except Exception as e:
        print(f"Maybe it doesn't have blocks? {str(e)}")

    print("validated service id -> ", validate_id.hexdigest())


def compile_directory(directory: str):
    service_zip_dir: str = __generate_service_zip(
        project_directory=directory
    )

    # TODO check if dependencies has directories, and compile them before.
    next((
        __on_peer(peer=f"{ip}:{port}", service_zip_dir=service_zip_dir)
        for peer_id in get_peer_ids()
        for ip, port in get_peer_directions(peer_id=peer_id)
    ))
