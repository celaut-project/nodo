import shutil
from time import sleep, time

import grpc
import json
import threading
from grpcbigbuffer.client import Dir, client_grpc

from tests.protos import api_pb2, api_pb2_grpc
from protos import gateway_pb2, celaut_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from tests.main import RANDOM, SORTER, FRONTIER, GATEWAY, to_gas_amount, SHA3_256

LIST_OF_SOLVERS = [FRONTIER]


def is_good(cnf, interpretation):
    def good_clause(clause, interpretation):
        for var in clause.literal:
            for i in interpretation.variable:
                if var == i:
                    return True
        return False

    for clause in cnf.clause:
        if not good_clause(clause, interpretation):
            return False
    return True


def generator(_hash: str, mem_limit: int = 50 * pow(10, 6), initial_gas_amount: int = pow(10, 16)):
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
    except Exception as _e:
        print(_e)

    yield Dir(
        dir='__metadata__/' + _hash,
        _type=celaut_pb2.Any.Metadata
    )

    yield Dir(
        dir='__registry__/' + _hash,
        _type=celaut_pb2.Service
    )


# Start the script.

if input('Clean json data? (y/n)') == 'y':
    print('\nClearing data ...')
    with open('script_data.json', 'w') as file:
        json.dump("", file)

g_stub = gateway_pb2_grpc.GatewayStub(
    grpc.insecure_channel(GATEWAY + ':8090')
)

sleep(5)

print(json.load(open('script_data.json', 'r')))
if type(json.load(open('script_data.json', 'r'))) != dict:

    print('Get new services....')

    # Get a classifier.
    classifier = next(client_grpc(
        method=g_stub.StartService,
        input=generator(
            _hash=SORTER,
            mem_limit=105 * pow(10, 6),
            initial_gas_amount=pow(10, 64)
        ),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=StartService_input_indices
    ))
    print(classifier)
    uri = classifier.instance.uri_slot[0].uri[0]
    c_uri = uri.ip + ':' + str(uri.port)
    c_stub = api_pb2_grpc.SolverStub(
        grpc.insecure_channel(c_uri)
    )
    print('Tenemos clasificador. ', c_stub)

    # Get random cnf
    random_cnf_service = next(client_grpc(
        method=g_stub.StartService,
        input=generator(_hash=RANDOM),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=StartService_input_indices
    ))
    print(random_cnf_service)
    uri = random_cnf_service.instance.uri_slot[0].uri[0]
    r_uri = uri.ip + ':' + str(uri.port)
    r_stub = api_pb2_grpc.RandomStub(
        grpc.insecure_channel(r_uri)
    )
    print('Tenemos random. ', r_stub)

    if FRONTIER != '':
        # Get the frontier for test it.
        frontier_service = next(client_grpc(
            method=g_stub.StartService,
            input=generator(_hash=FRONTIER),
            indices_parser=gateway_pb2.Instance,
            partitions_message_mode_parser=True,
            indices_serializer=StartService_input_indices,
        ))
        uri = frontier_service.instance.uri_slot[0].uri[0]
        frontier_uri = uri.ip + ':' + str(uri.port)
        frontier_stub = api_pb2_grpc.SolverStub(
            grpc.insecure_channel(frontier_uri)
        )

        print('Tenemos frontier ', frontier_stub)

    # save stubs on json.
    with open('script_data.json', 'w') as file:
        json.dump({
            'sorter': c_uri,
            'random': r_uri,
            'frontier': frontier_uri,
        }, file)

else:
    print('Getting from json file.')
    with open('script_data.json', 'r') as file:
        data = json.load(file)
        print(data)
        c_stub = api_pb2_grpc.SolverStub(
            grpc.insecure_channel(data['sorter'])
        )
        r_stub = api_pb2_grpc.RandomStub(
            grpc.insecure_channel(data['random'])
        )
        try:
            frontier_stub = api_pb2_grpc.SolverStub(
                grpc.insecure_channel(data['frontier'])
            )
        except:
            pass

sleep(10)  # Espera a que el servidor se levante.

print('Subiendo solvers al clasificador.')
for s in LIST_OF_SOLVERS:
    print('     ', s)
    while True:
        try:
            next(client_grpc(
                method=c_stub.UploadSolver,
                indices_serializer={
                    1: celaut_pb2.Any.Metadata,
                    2: celaut_pb2.Service,
                },
                input=(
                    Dir(dir='__metadata__/' + s, _type=celaut_pb2.Any.Metadata),
                    Dir(dir='__registry__/' + s, _type=celaut_pb2.Service)
                )
            ))
            break
        except Exception as e:
            print('Error al conectar con el clasificador ', e)
            sleep(1)
try:
    dataset = solvers_dataset_pb2.DataSet()
    dataset.ParseFromString(open('dataset.bin', 'rb').read())
    next(client_grpc(
        method=c_stub.AddDataSet,
        input=dataset
    ))
    print('Dataset añadido.')
except Exception as e:
    print('No tenemos dataset.')
    pass

if True:  # input("\nGo to train? (y/n)")=='y':
    print('Iniciando entrenamiento...')
    # Inicia el entrenamiento.
    next(client_grpc(method=c_stub.StartTrain))

    print('Wait to train the model ...')
    for i in range(50):
        for j in range(5):
            print(f'\n{i}-{j}')
            sleep(200)

        cnf = next(client_grpc(
            method=r_stub.RandomCnf,
            partitions_message_mode_parser=True,
            indices_parser=api_pb2.Cnf
        ))
        # Comprueba si sabe generar una interpretacion (sin tener ni idea de que tal
        # ha hecho la seleccion del solver.)
        print('\n ---- ', i)

        print(' SOLVING CNF ...')
        t = time()
        try:
            interpretation = next(client_grpc(
                method=c_stub.Solve,
                indices_parser={1: api_pb2.Interpretation, 2: api_pb2.Empty},
                partitions_message_mode_parser=True,
                input=cnf,
                indices_serializer=api_pb2.Cnf,
            ))
            if type(interpretation) is api_pb2.Empty:
                print("No interpretation")
            else:
                print(str(time() - t) + ' OKAY THE INTERPRETATION WAS ', interpretation, '.',
                      is_good(cnf=cnf, interpretation=interpretation))

            print(' SOLVING CNF ON DIRECT SOLVER ...')
            t = time()
            try:
                interpretation = next(client_grpc(
                    method=frontier_stub.Solve,
                    input=cnf,
                    indices_serializer=api_pb2.Cnf,
                    partitions_message_mode_parser=True,
                    indices_parser=api_pb2.Interpretation
                ))
            except(grpc.RpcError):
                # Get the frontier for test it.
                uri = next(client_grpc(
                    method=g_stub.StartService,
                    input=generator(_hash=FRONTIER),
                    indices_parser=gateway_pb2.Instance,
                    partitions_message_mode_parser=True,
                    indices_serializer=StartService_input_indices
                )).instance.uri_slot[0].uri[0]
                frontier_uri = uri.ip + ':' + str(uri.port)
                frontier_stub = api_pb2_grpc.SolverStub(
                    grpc.insecure_channel(frontier_uri)
                )
                interpretation = next(client_grpc(
                    method=frontier_stub.Solve,
                    input=cnf,
                    indices_serializer=api_pb2.Cnf,
                    partitions_message_mode_parser=True,
                    indices_parser=api_pb2.Interpretation
                ))
            print(str(time() - t) + ' OKAY THE FRONTIER SAID ', interpretation, '.',
                  is_good(interpretation=interpretation, cnf=cnf))

        except Exception as e:
            print('Solving cnf error -> ', str(e), ' no debe de tener listo el tensor.')

        print('Obtiene el data_set.')

        dataset_obj = next(client_grpc(
            method=c_stub.GetDataSet,
            indices_parser=solvers_dataset_pb2.DataSet,
            partitions_message_mode_parser=False
        ))
        shutil.copyfile(
            dataset_obj.dir,
            'dataset.bin'
        )

    sleep(100)

print('Termina el entrenamiento')
# En caso de que estubiera entrenando lo finaliza.
next(client_grpc(method=c_stub.StopTrain))


# Comprueba si sabe generar una interpretacion (sin tener ni idea de que tal
# ha hecho la seleccion del solver.)
def final_test(c_stub, r_stub, i, j):
    cnf = next(client_grpc(method=r_stub.RandomCnf, indices_parser=api_pb2.Cnf, partitions_message_mode_parser=True))
    t = time()
    interpretation = next(client_grpc(
        method=c_stub.Solve,
        input=cnf,
        indices_serializer=api_pb2.Cnf,
        partitions_message_mode_parser=True,
        indices_parser=api_pb2.Interpretation
    ))
    print(interpretation, str(time() - t) + 'THE FINAL INTERPRETATION IN THREAD ' + str(threading.get_ident()),
          ' last time ', i, j)


for i in range(3):
    sleep(10)
    threads = []
    for j in range(10):
        t = threading.Thread(target=final_test, args=(c_stub, r_stub, i, j,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

"""
print('Obtiene el tensor.')
counter = 6
for tensor in c_stub.GetTensor(api_pb2.Empty()):
    print('\n\nTENSOR -> ', tensor)
    sleep(10)
    if counter==0: break
    else: counter-=1
"""

print('Obtiene el data_set.')
dataset_it = client_grpc(
    method=c_stub.GetDataSet,
    indices_parser=solvers_dataset_pb2.DataSet,
    partitions_message_mode_parser=False
)
dataset_obj = next(dataset_it)
print(dataset_obj, type(dataset_obj))
if type(dataset_obj) != str:
    dataset_obj = next(dataset_it)
    print(dataset_obj, type(dataset_obj))
shutil.copyfile(
    dataset_obj,
    'dataset.bin'
)

print('waiting for kill solvers ...')
for i in range(10):
    for j in range(10):
        sleep(10)
        print(i, j)

# Stop the classifier.
print('Stop the clasifier.')
try:
    next(client_grpc(
        method=g_stub.StopService,
        input=gateway_pb2.TokenMessage(token=classifier.token)
    ))
except Exception as e:
    print('e -> ', e)

# Stop Random cnf service.
print('Stop the random.')
try:
    next(client_grpc(
        method=g_stub.StopService,
        input=gateway_pb2.TokenMessage(token=random_cnf_service.token)
    ))
except Exception as e:
    print('e -> ', e)

# Stop Frontier cnf service.
print('Stop the frontier.')
try:
    next(client_grpc(
        method=g_stub.StopService,
        input=gateway_pb2.TokenMessage(token=frontier_service.token)
    ))
except Exception as e:
    print('e -> ', e)

print('All good?')

with open('script_data.json', 'w') as file:
    print('Clearing data ...')
    json.dump("", file)
