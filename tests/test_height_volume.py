import sys
from random import choice
from threading import Thread
from time import sleep, time

import grpc
from grpcbigbuffer import buffer_pb2
from grpcbigbuffer.client import Dir, client_grpc

from protos import gateway_pb2, celaut_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from tests.main import FRONTIER, GATEWAY, RANDOM, SHA3_256
from tests.protos import api_pb2_grpc, api_pb2


def generator(hash: str, mem_limit: int = 50 * pow(10, 6)):
    try:

        # 1
        yield gateway_pb2.Client(client_id='dev')

        # 3
        yield gateway_pb2.Configuration(
            config=celaut_pb2.Configuration(),
            min_sysreq=celaut_pb2.Sysresources(
                mem_limit=mem_limit
            )
        )

        # 4
        yield celaut_pb2.Any.Metadata.HashTag.Hash(
            type=bytes.fromhex(SHA3_256),
            value=bytes.fromhex(hash)
        )

        # 6
        yield Dir(dir='__registry__/' + hash, _type=celaut_pb2.Service)

    except Exception as e:
        print(e)


def service_extended(hash):
    yield from generator(hash=hash)


def get_grpc_uri(instance: celaut_pb2.Instance) -> celaut_pb2.Instance.Uri:
    for slot in instance.api.slot:
        # if 'grpc' in slot.transport_protocol.metadata.tag and 'http2' in slot.transport_protocol.metadata.tag:
        # If the protobuf lib. supported map for this message it could be O(n).
        for uri_slot in instance.uri_slot:
            if uri_slot.internal_port == slot.port:
                return uri_slot.uri[0]
    raise Exception('Grpc over Http/2 not supported on this service ' + str(instance))


g_stub = gateway_pb2_grpc.GatewayStub(

    grpc.insecure_channel(GATEWAY),
)


def exec(id: int, solver_hash: str):
    print(id, '  Go to use ', solver_hash)
    # Get solver cnf
    random = next(client_grpc(
        method=g_stub.StartService,
        input=service_extended(hash=RANDOM),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=StartService_input_indices,
    ))

    print('random ', id, ' -> ', random)

    random_uri = get_grpc_uri(random.instance)
    random_stub = api_pb2_grpc.RandomStub(

        grpc.insecure_channel(
            random_uri.ip + ':' + str(random_uri.port)
        )
    )
    random_token = random.token

    solver = next(client_grpc(
        method=g_stub.StartService,
        input=service_extended(hash=solver_hash),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=StartService_input_indices,
    ))

    print('SOLVER ', id, ' -> ', solver)

    solver_uri = get_grpc_uri(solver.instance)

    solver_stub = api_pb2_grpc.SolverStub(
        grpc.insecure_channel(
            solver_uri.ip + ':' + str(solver_uri.port)
        )
    )
    solver_token = solver.token

    print(' SOLVER SERVICE ', id, ' -> ', solver_uri)

    print('\nwait.', id, ' ..')
    print('\n\nTest it  ', id, ' on ', solver_uri)
    i: int = 0
    while i < 5:
        try:
            cnf = next(client_grpc(
                method=random_stub.RandomCnf,
                indices_parser=api_pb2.Cnf,
                partitions_message_mode_parser=True,
                input=buffer_pb2.Empty()
            ))
            print('cnf -> ', cnf)
            break
        except Exception as e:
            i -= 1
            print('ERROR LAUNCHING CNF')
            sleep(1)

    while i < 10:
        interpretation = None
        try:
            interpretation = next(client_grpc(
                method=solver_stub.Solve,
                input=cnf,
                indices_serializer=api_pb2.Cnf,
                indices_parser=api_pb2.Interpretation,
                partitions_message_mode_parser=True
            ))
            break
        except Exception as e:
            i -= 1
            print('ERROR LAUNCHING SOLVER')
            sleep(1)

    if interpretation: print('Interpretation  ', id, ' -- ', i, ' -> ', interpretation)
    sleep(10)
    print('Go to stop that  ', id, ' .', random_token, solver_token)
    next(client_grpc(
        method=g_stub.StopService,
        input=gateway_pb2.TokenMessage(
            token=random_token
        ),
        indices_parser=buffer_pb2.Empty,
    ))
    next(client_grpc(
        method=g_stub.StopService,
        input=gateway_pb2.TokenMessage(
            token=solver_token
        ),
        indices_parser=buffer_pb2.Empty,
    ))
    print('Stopped ', id)


thread_list = []
start_time = int(time())
ri: int = int(sys.argv[1])
i: int = 0
while i < ri:
    # sleep(randint(0, ri))
    t = Thread(
        target=exec,
        args=(i, choice([FRONTIER]))
    )
    t.start()
    i += 1

    thread_list.append(t)
for t in thread_list:
    t.join()
print('\n\n TEST PASSED.', int(time()) - start_time)
