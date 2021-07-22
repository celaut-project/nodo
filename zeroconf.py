import grpc
import gateway_pb2_grpc, grpc


def Zeroconf(local_instance):

    IPVERSION_IS_4 = True
    get_peer_ip = lambda peer: peer.split(':')[-2] if IPVERSION_IS_4 else peer[1:].split(']')[0]
        
    # an ipv4 with port is 126.4.5.7:8034
    # an ipv6 with port is [2001:db8:1f70::999:de8:7648:6e8]:8034

    peers_grpc_http2 = ['192.168.0.1:8080']
    peers_http1 = []

    total_peers = []
    instances = []


    # Get peers
    #  fill peers list using zeroconf protocol (ip:port that use hy method using Grpc over Http/2)
    #  peers_grpc_http2.append()

    # Check what is the IP version.

    # Check if is a hynode with grpc over http/2.
    for peer in peers_grpc_http2:
        peer_ip = get_peer_ip(peer)
        if peer_ip not in total_peers:
            total_peers.append(peer_ip)
            instances.append (
                gateway_pb2_grpc.Gateway(
                    grpc.insecure_channel(peer)
                ).Hynode(local_instance)
            )
        
    # Check if is a hynode with http/1.
    for peer in peers_http1:
        pass


    return instances