import socket, struct
import ipss_pb2, gateway_pb2
import netifaces as ni

def get_grpc_uri(instance: ipss_pb2.Instance) -> ipss_pb2.Instance.Uri:
    #  Supone que el primer slot usa grpc sobre http/2.
    for slot in instance.api.slot:
        if 'grpc' in slot.transport_protocol.hash and 'http2' in slot.transport_protocol.hash:
            return instance.uri_slot[slot.port]
    raise Exception('Grpc over Http/2 not supported on this service ' + str(instance))

def service_extended(service: gateway_pb2.ipss__pb2.Service, config: gateway_pb2.ipss__pb2.Configuration, ):
    set_config = True
    transport = gateway_pb2.ServiceTransport()
    for hash in service.hash:
        transport.hash = hash
        if set_config:  # Solo hace falta enviar la configuracion en el primer paquete.
            transport.config.CopyFrom(config)
            set_config = False
        yield transport
    transport.ClearField('hash')
    if set_config: transport.config.CopyFrom(config)
    transport.service.CopyFrom(service)
    yield transport

def get_free_port():
    with socket.socket() as s:
        s.bind(('', 0))
        return int(s.getsockname()[1])

get_only_the_ip_from_context = lambda context_peer: context_peer[5:-1*(len(context_peer.split(':')[-1])+1)] if context_peer.slit(':')[0] == 'ipv4' else None  # Lleva el formato 'ipv4:49.123.106.100:4442', no queremos 'ipv4:' ni el puerto.

get_local_ip_from_network = lambda network: ni.ifaddresses(network)[ni.AF_INET][0]['addr']

def makeMask(n):
    "return a mask of n bits as a long integer"
    return (2L<<n-1) - 1

def dottedQuadToNum(ip):
    "convert decimal dotted quad string to long integer"
    return struct.unpack('L',socket.inet_aton(ip))[0]

def networkMask(ip, bits):
    "Convert a network address to a long integer" 
    return dottedQuadToNum(ip) & makeMask(bits)

def addressInNetwork(ip, net):
   "Is an address in a network"
   return ip & net == net

def get_network_name(ip:str) -> str:
    #  https://stackoverflow.com/questions/819355/how-can-i-check-if-an-ip-is-in-a-network-in-python
    for network in ni.interfaces():
        if addressInNetwork(
            ip=dottedQuadToNum(
                ip=ip
            ),
            net=networkMask(
                ip=get_local_ip_from_network(network),
                bits=24
            )
        ): return network