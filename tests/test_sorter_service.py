import os
import shutil
import sys
from time import sleep, time
from typing import Final, Optional

import grpc
import json
import threading
from grpcbigbuffer.client import Dir, client_grpc

from src.utils.env import REGISTRY, METADATA_REGISTRY, STORAGE
from tests.protos import api_pb2, api_pb2_grpc
from protos import gateway_pb2, celaut_pb2, gateway_pb2_grpc
from tests.protos import solvers_dataset_pb2
from protos.gateway_pb2_grpcbf import StartService_input_indices
from tests.main import RANDOM, SORTER, FRONTIER, GATEWAY, generator as gen

LIST_OF_SOLVERS = [FRONTIER]
TEST_DATA_STORAGE = f'{STORAGE}/tests'
os.makedirs(TEST_DATA_STORAGE, exist_ok=True)
SESSION_SERVICES_JSON: Final[str] = f'{TEST_DATA_STORAGE}/script_data.json'

def test_sorter_service(sorter_endpoint: Optional[str] = sys.argv[3] if len(sys.argv) == 4 else None):
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
        yield from gen(_hash=_hash, mem_limit=mem_limit, initial_gas_amount=initial_gas_amount)

    # Start the script.

    if input('Clean json data? (y/n)') == 'y':
        print('\nClearing data ...')
        with open(SESSION_SERVICES_JSON, 'w') as file:
            json.dump("", file)

    g_stub = gateway_pb2_grpc.GatewayStub(
        grpc.insecure_channel(GATEWAY)
    )

    sleep(5)

    print(json.load(open(SESSION_SERVICES_JSON, 'r')))
    if type(json.load(open(SESSION_SERVICES_JSON, 'r'))) != dict:

        print('Get new services....')

        if not sorter_endpoint:
            if not os.path.exists(os.path.join(REGISTRY, SORTER)):
                raise Exception(f"{SORTER} service not exists.")
            print(f"Get the sorter {SORTER}")
            try:
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
            except Exception as e:
                print(f"Exception getting the sorter service -> {e}")
                raise e
            print(classifier)
            uri = classifier.instance.uri_slot[0].uri[0]
            c_uri = uri.ip + ':' + str(uri.port)
        else:
            c_uri = sorter_endpoint
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
        print('Received random. ', r_stub)

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

            print('Received frontier ', frontier_stub)

        # save stubs on json.
        with open(SESSION_SERVICES_JSON, 'w') as file:
            json.dump({
                'sorter': c_uri,
                'random': r_uri,
                'frontier': frontier_uri,
            }, file)

    else:
        print('Getting from json file.')
        with open(SESSION_SERVICES_JSON, 'r') as file:
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

    print('Uploading solvers al clasificador.')
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
                        Dir(dir=os.path.join(METADATA_REGISTRY, s), _type=celaut_pb2.Any.Metadata),
                        Dir(dir=os.path.join(REGISTRY, s), _type=celaut_pb2.Service)
                    )
                ))
                break
            except Exception as e:
                print('Error connecting to the classifier ', e)
                sleep(1)
    try:
        dataset = solvers_dataset_pb2.DataSet()
        dataset.ParseFromString(open('dataset.bin', 'rb').read())
        next(client_grpc(
            method=c_stub.AddDataSet,
            input=dataset
        ))
        print('Dataset added.')
    except Exception as e:
        print("Don't have dataset")
        pass

    if True:  # input("\nGo to train? (y/n)")=='y':
        print('Starting training ...')
        next(client_grpc(method=c_stub.StartTrain))

        print('Wait to train the model ...')
        for i in range(200):

            sleep(5)

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
                print('Solving cnf error -> ', str(e), ' may the tensor is not ready')

            print('Received the data_set.')

            dataset_obj = next(client_grpc(
                method=c_stub.GetDataSet,
                indices_parser=solvers_dataset_pb2.DataSet,
                partitions_message_mode_parser=False
            ))
            shutil.copyfile(
                dataset_obj.dir,
                'dataset.bin'
            )

            sleep(5)

        sleep(100)

    print('Ends the training')
    next(client_grpc(method=c_stub.StopTrain))

    # "Check if it knows how to generate an interpretation (without having any idea how the solver selection was done)."
    def final_test(c_stub, r_stub, i, j):
        cnf = next(
            client_grpc(method=r_stub.RandomCnf, indices_parser=api_pb2.Cnf, partitions_message_mode_parser=True))
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

    print('Received the data_set.')
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
    print('Stop the classifier.')
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

    with open(SESSION_SERVICES_JSON, 'w') as file:
        print('Clearing data ...')
        json.dump("", file)
