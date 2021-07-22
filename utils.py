import socket
import ipss_pb2, gateway_pb2
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