from protos import gateway_pb2_grpc, gateway_pb2
from bee_rpc.client import client_grpc as client

import grpc

from src.manager.manager import add_peer_instance
from src.tunneling_system.tunnels import TunnelSystem
from src.utils import logger as log
from src.gateway.utils import generate_gateway_instance
from src.database.sql_connection import SQLConnection
from src.utils.utils import get_network_name

SEND_INSTANCE = False  # TODO Variable, true only in case of  ERG amount not sufficient to send instance to the reputation system.

sc = SQLConnection()

def connect(peer: str):
    print('Connecting to peer ->', peer)

    if sc.uri_exists(uri=peer):
        print(f"Peer {peer} is already registered.")
        return

    try:
        # Call the appropriate function to insert the instance into the SQLite database
        add_peer_instance(
            next(client(
                method=gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(peer)
                ).GetPeerInfo,
                indices_parser=gateway_pb2.Peer,
                partitions_message_mode_parser=True
            ))
        )
        print('\nAdded peer', peer)
        
        if SEND_INSTANCE:
            print(f'Sending instance to peer: {peer}')
            
            try:
                # Could be refactored with Gateway.GetPeerInfo
                if TunnelSystem().from_tunnel(ip=peer):
                    gateway_instance = TunnelSystem().get_gateway_tunnel()
                else:
                    gateway_instance = generate_gateway_instance(
                        network=get_network_name(direction=peer)
                    )
            except Exception as e:
                print(f"Error generating instance for peer {peer}. {e}")
                return
            
            try:
                _result = next(client(
                    method=gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer)
                    ).IntroducePeer,
                    indices_serializer=gateway_pb2.Peer,
                    input=gateway_instance,
                    indices_parser=gateway_pb2.RecursionGuard,  # Recursion guard shouldn't be used here, another message should be used. TODO
                    partitions_message_mode_parser=True
                ))
                
                print(_result)
                
            except Exception as e:
                print(f"Error sending instance to peer {peer}. {e}")
            
    except Exception as e:
        print(e)
