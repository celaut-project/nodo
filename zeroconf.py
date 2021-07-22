from DEFAULT_PEERS import default_grpc_http2__peers, default_http1__peers
import gateway_pb2_grpc, grpc


def Zeroconf(local_instance) -> list:

    IPVERSION_IS_4 = True
    get_peer_ip = lambda peer: peer.split(':')[-2] if IPVERSION_IS_4 else peer[1:].split(']')[0]
        
    # an ipv4 with port is 126.4.5.7:8034
    # an ipv6 with port is [2001:db8:1f70::999:de8:7648:6e8]:8034

    grpc_http2__peers = default_grpc_http2__peers if len(default_grpc_http2__peers)>0 else []
    http1__peers = default_http1__peers if len(default_http1__peers)>0 else []

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
            peer_instances.append (
                gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(peer_uri)
                ).Hynode(local_instance)
            )
        
    # Check if is a hynode with http/1.
    for peer_uri in http1__peers:
        pass


    return peer_instances