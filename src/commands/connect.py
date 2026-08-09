from protos import celaut_pb2_grpc, celaut_pb2
from bee_rpc.client import client_grpc as client

import grpc

from src.manager.manager import add_peer_instance
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager

env_manager = ConfigManager()
SELF_ANNOUNCE_TO_CONNECTING_PEERS = env_manager.get("SELF_ANNOUNCE_TO_CONNECTING_PEERS")

sc = SQLConnection()

def connect(peer: str):
    print('Connecting to peer ->', peer)

    # A known peer is re-handshaked rather than skipped: that is how a payment
    # contract it only started advertising later gets registered locally.
    if sc.uri_exists(uri=peer):
        print(f"Peer {peer} is already registered; refreshing what it advertises.")

    try:
        channel = grpc.insecure_channel(peer)
        try:
            peer_info = next(client(
                    method=celaut_pb2_grpc.GatewayStub(channel).GetPeerInfo,
                    indices_parser=celaut_pb2.Peer,
                    partitions_message_mode_parser=True
                ))
        finally:
            channel.close()
        
        peer_id = add_peer_instance(peer_info)
        if not peer_id:
            print("Failed to add a peer.")
        else:
            print(f'Added peer {peer} with id {peer_id}')
            if not peer_info.api.payment_contracts:
                print(
                    f"Note: peer {peer} advertises no payment contract, so it cannot "
                    "be paid yet (its ledger interface may not be initialised)."
                )

        if SELF_ANNOUNCE_TO_CONNECTING_PEERS:
            from src.gateway.utils import generate_full_node_peer_info
            print(f'Sending instance to peer: {peer}')

            try:
                gateway_instance = generate_full_node_peer_info()
            except Exception as e:
                print(f"Error generating instance for peer {peer}. {e}")
                return
            
            try:
                channel = grpc.insecure_channel(peer)
                try:
                    _result = next(client(
                        method=celaut_pb2_grpc.GatewayStub(channel).IntroducePeer,
                        indices_serializer=celaut_pb2.Peer,
                        input=gateway_instance,
                        indices_parser=celaut_pb2.RecursionGuard,  # Recursion guard shouldn't be used here, another message should be used. TODO
                        partitions_message_mode_parser=True
                    ))
                finally:
                    channel.close()
                
                print(_result)
                
            except Exception as e:
                print(f"Error sending instance to peer {peer}. {e}")
            
    except Exception as e:
        print(f"Error connecting to peer {peer}. {e}")
