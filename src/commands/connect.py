from protos import celaut_pb2_grpc, celaut_pb2
from bee_rpc.client import client_grpc as client

import grpc

from src.manager.manager import add_peer_instance
from src.tunneling_system.tunnels import TunnelSystem
from src.gateway.utils import generate_node_peer_info
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.utils import get_network_name

env_manager = ConfigManager()
SELF_ANNOUNCE_TO_CONNECTING_PEERS = env_manager.get("SELF_ANNOUNCE_TO_CONNECTING_PEERS")

sc = SQLConnection()

def connect(peer: str):
    print('Connecting to peer ->', peer)

    if sc.uri_exists(uri=peer):
        print(f"Peer {peer} is already registered.")
        return

    try:
        peer_info = next(client(
                method=celaut_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(peer)
                ).GetPeerInfo,
                indices_parser=celaut_pb2.Peer,
                partitions_message_mode_parser=True
            ))
        
        peer_id = add_peer_instance(peer_info)
        if not peer_id:
            print("Failed to add a peer.")
            
        print(f'Added peer {peer} with id {peer_id}')
        
        if SELF_ANNOUNCE_TO_CONNECTING_PEERS:
            print(f'Sending instance to peer: {peer}')
            
            try:
                # Could be refactored with Gateway.GetPeerInfo
                if TunnelSystem().from_tunnel(ip=peer):
                    gateway_instance = TunnelSystem().get_gateway_tunnel()
                else:
                    gateway_instance = generate_node_peer_info(
                        network=get_network_name(direction=peer)
                    )
            except Exception as e:
                print(f"Error generating instance for peer {peer}. {e}")
                return
            
            try:
                _result = next(client(
                    method=celaut_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer)
                    ).IntroducePeer,
                    indices_serializer=celaut_pb2.Peer,
                    input=gateway_instance,
                    indices_parser=celaut_pb2.RecursionGuard,  # Recursion guard shouldn't be used here, another message should be used. TODO
                    partitions_message_mode_parser=True
                ))
                
                print(_result)
                
            except Exception as e:
                print(f"Error sending instance to peer {peer}. {e}")
            
    except Exception as e:
        print(f"Error connecting to peer {peer}. {e}")
