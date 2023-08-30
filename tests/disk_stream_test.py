from typing import List

from protos import gateway_pb2
from grpcbigbuffer import buffer_pb2
import hashlib

SERVICE_DIR = '__registry__/b0fc076a49adb3d5e03b76996cfe82c81efba9f2115d343a39ee46883c5fdc35'


def generate_partitions(
        element: gateway_pb2.ServiceWithMeta,
        model: List[buffer_pb2.Buffer.Head.Partition]
) -> List[str]:

    def generator(e):
        yield e


    import grpcbigbuffer as grpcbb

    g = grpcbb.parse_from_buffer(
        request_iterator = grpcbb.serialize_to_buffer(
                                message_iterator=generator(e=element),
                                indices=gateway_pb2.ServiceWithMeta
                            ),
        indices = gateway_pb2.ServiceWithMeta,
        partitions_model = model,
        partitions_message_mode = False,
        yield_remote_partition_dir = False,
    )

    return [i for i in g][ 1 if len(model) > 1 else 0 :]

#################
#### TESTS ######
#################
from protos import gateway_pb2_grpcbf

from tests.disk_stream import validate_partitions, calculate_hash_of_complete, partition_disk_stream, generate_partitions, \
    encode_bytes


def check_validate_function():
    assert validate_partitions(
        partitions=gateway_pb2_grpcbf.StartService_input_partitions_v2[2]
    ) == False

    assert validate_partitions(
        partitions=gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
    ) == True


def check_hash_calculation_with_small_service():
    # Try to test it commenting some attributes manually.
    from random import  randint

    for i in range(0, 300):
        element = gateway_pb2.ServiceWithMeta()
        if randint(0,1):
            for r in range(0, randint(0, 99)):
                element.metadata.hashtag.tag.append(str(r))
        else:
            element.metadata.CopyFrom(gateway_pb2.celaut__pb2.Any.Metadata())
            # Si pasamos la partición vacía la función considera que el buffer posee el íncide con longitud 0.
            #  Ej: b'\n\x00'
            # Si no se desea poner, el esquema de particiones no debe de poseerla.

        if randint(0,1):
            element.service.container.architecture = b''.join([b'x86' for i in range(1, randint(1, 1000000))])
        elif randint(0,1):
            element.service.container.filesystem = b''.join([b'python' for i in range(1, randint(1, 1000000))])
        elif randint(0,1):
            for r in range(1, randint(1, 1000000)):
                element.service.container.entrypoint.append(str(r))
        else:
            for r in range(1, randint(1, 1000000)):
                element.service.container.enviroment_variables[str(r)].CopyFrom(gateway_pb2.celaut__pb2.FieldDef())


        try:
            p1: bytes = b''.join([chunk for chunk in
                    partition_disk_stream(
                        dirs = generate_partitions(
                            element = element,
                            model = gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
                        ),
                        partitions=gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
                    )
                ])
        except Exception as e:
            print(str(e), element)
            return

        p2: bytes = element.SerializeToString()

        try:
            assert p1 == p2
        except AssertionError:
            print('small not passed.')

        hash_partition: str = calculate_hash_of_complete(
            dirs=generate_partitions(
                element=element,
                model=gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
            ),
            partitions=gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
        )

        assert hash_partition == hashlib.sha3_256(p2).hexdigest()
        print(i, ' passed.')

def check_hash_calculation_with_big_service():
    element = gateway_pb2.ServiceWithMeta()
    element.ParseFromString(open(SERVICE_DIR, 'rb').read())
    h1 = calculate_hash_of_complete(
        dirs = generate_partitions(
                    element=element,
                    model=gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
                ),
        partitions = gateway_pb2_grpcbf.StartService_input_partitions_v3[2]
    )
    h2 = calculate_hash_of_complete(
        dirs = [SERVICE_DIR]
    )

    try:
        assert h1 == h2
        print('big passed.')
    except AssertionError:
        print('big not passed.')
        print(h1)
        print(h2)


check_hash_calculation_with_small_service()
check_hash_calculation_with_big_service()