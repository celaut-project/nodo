import gateway_pb2_grpc, grpc
from gateway import LOGGER, generate_gateway_instance, insert_instance_on_mongo
from utils import get_network_name

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
                peer_instances.append (
                    gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer_uri)
                    ).Hynode(
                        generate_gateway_instance(
                            network=network
                        )
                    )
                )
            except grpc.RpcError:
                LOGGER('Node ' + peer_uri + ' not response.')
                continue
        
    # Check if is a hynode with http/1.
    for peer_uri in http1__peers:
        pass

    # Insert the instances.
    for peer_instance in peer_instances:
        insert_instance_on_mongo(instance=peer_instance)


if __name__ == "__main__":
    import sys
    insert_instance_on_mongo(
        instance = gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(
                    sys.argv[1]
                )
            ).Hynode(
                generate_gateway_instance(
                    network=get_network_name(ip=sys.argv[1])
                )
            )
    )
    LOGGER('\nAdded peer ' + sys.argv[1])