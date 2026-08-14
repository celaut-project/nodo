from protos import celaut_pb2_grpc, celaut_pb2
from bee_rpc.client import client_grpc as client

import grpc

from src.manager.manager import add_peer_instance, verified_peer_public_key
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
            if not verified_peer_public_key(peer_info):
                # Distinguish a policy refusal from a failure: this peer answered, it
                # just did not prove an identity, and no amount of retrying will change
                # that. Asking the same question add_peer_instance asked, rather than
                # only checking whether the fields are empty: a peer running the older
                # ECDSA scheme *does* send both, and it is refused all the same -- so
                # keying off emptiness would send it down the retry-implying branch.
                # A node signs with the identity key derived from its wallet mnemonic,
                # so one that cannot be verified is running code that predates it.
                print(
                    f"Refused peer {peer}: it did not prove an identity, so there is no "
                    "key to register it under (see the log for which check failed)."
                )
            else:
                print("Failed to add a peer.")
        else:
            print(f'Added peer {peer} with id {peer_id}')
            # We dialled this address and it answered with an identity we verified, so
            # it is this peer's and nobody else's. Any other peer still holding it is
            # stale (the usual cause: the same host regenerated its mnemonic, so its
            # peer_id changed) and would otherwise keep being tried first.
            for previous_id in sc.claim_uri(uri=peer, peer_id=peer_id):
                print(
                    f"Endpoint {peer} was registered under peer {previous_id}; removed it "
                    "from that peer, which now answers at whatever other addresses it "
                    "announced (`nodo disconnect` it if there are none)."
                )
            if not peer_info.payment_contracts:
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
                
                if _result.token == "REFUSED":
                    # The peer stored nothing: it could not verify this node's identity
                    # signature. Worth saying out loud -- the announcement is what makes
                    # this node reachable, and the log line for it is on the remote side.
                    print(
                        f"Peer {peer} refused this node's announcement; it could not "
                        "verify our identity signature."
                    )
                else:
                    print(f"Peer {peer} accepted this node's announcement: {_result.token}")

            except Exception as e:
                print(f"Error sending instance to peer {peer}. {e}")
            
    except Exception as e:
        print(f"Error connecting to peer {peer}. {e}")
