from protos.gateway_pb2 import Instance
from protos import gateway_pb2_grpc
from grpcbigbuffer.client import client_grpc
import sqlite3
import uuid
import grpc

from src.manager.manager import insert_instance_on_db
from src.utils import logger as l
from src.gateway.utils import generate_gateway_instance


def Zeroconf(network: str) -> list:
    ipversion_is_4 = True
    get_peer_ip = lambda peer: peer.split(':')[-2] if ipversion_is_4 else peer[1:].split(']')[0]

    # an ipv4 with port is 126.4.5.7:8034
    # an ipv6 with port is [2001:db8:1f70::999:de8:7648:6e8]:8034

    grpc_http2__peers = []
    http1__peers = []

    total_peers = []
    peer_instances = []

    # Get peers
    #  fill peers list using zeroconf protocol (ip:port that use hy method using Grpc over Http/2)
    #  peers_grpc_http2.append()

    # Check what is the IP version.

    # Check if is a hynode with grpc over http/2.
    for peer_uri in grpc_http2__peers:
        peer_ip = get_peer_ip(peer_uri)
        if peer_ip not in total_peers:
            total_peers.append(peer_ip)
            try:
                peer_instances.append(
                    next(client_grpc(
                        method=gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(peer_uri)
                        ).GetInstance,
                        indices_parser=Instance,
                        input=generate_gateway_instance(
                            network=network
                        )
                    ))
                )
            except grpc.RpcError:
                l.LOGGER('Node ' + peer_uri + ' not response.')
                continue

    # Check if is a docker node with http/1.
    for peer_uri in http1__peers:
        pass

    # Insert the instances.
    for peer_instance in peer_instances:
        insert_instance_on_db(instance=peer_instance.instance)

    return peer_instances


def connect(peer: str):
    print('Connecting to peer ->', peer)

    # Connect to the SQLite database
    conn = sqlite3.connect('database.sqlite')
    cursor = conn.cursor()

    peer_id = str(uuid.uuid4())
    # Attempt to insert a new row into the 'peer' table
    cursor.execute("INSERT INTO peer (id) VALUES (?)", (peer_id,))
    conn.commit()

    print('Get instance for peer ->', peer_id)
    try:
        # Call the appropriate function to insert the instance into the SQLite database
        insert_instance_on_db(
            next(client_grpc(
                method=gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(peer)
                ).GetInstance,
                indices_parser=Instance,
                partitions_message_mode_parser=True
            )),
            _id=peer_id
        )
    except Exception as e:
        print(e)

    print('\nAdded peer', peer)

    # Close the database connection
    conn.close()
