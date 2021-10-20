import socket
from typing import Generator

import celaut_pb2, gateway_pb2
import netifaces as ni

def get_grpc_uri(instance: celaut_pb2.Instance) -> celaut_pb2.Instance.Uri:
    for slot in instance.api.slot:
        #if 'grpc' in slot.transport_protocol and 'http2' in slot.transport_protocol: # TODO
        # If the protobuf lib. supported map for this message it could be O(n).
        for uri_slot in instance.uri_slot:
            if uri_slot.internal_port == slot.port:
                return uri_slot.uri[0]
    raise Exception('Grpc over Http/2 not supported on this service ' + str(instance))

def service_hashes(
        hashes: list = []
    ) -> Generator[celaut_pb2.Any.Metadata.HashTag.Hash, None, None]:
        for hash in hashes:
            yield hash 

def service_extended(
        service: celaut_pb2.Service,
        metadata: celaut_pb2.Any.Metadata,  
        config: celaut_pb2.Configuration = None
    ) -> Generator[gateway_pb2.ServiceTransport, None, None]:
        set_config = True if config else False
        transport = gateway_pb2.ServiceTransport()
        for hash in metadata.hash:
            transport.hash.CopyFrom(hash)
            if set_config:  # Solo hace falta enviar la configuracion en el primer paquete.
                transport.config.CopyFrom(config)
                set_config = False
            yield transport
        transport.ClearField('hash')
        if set_config: transport.config.CopyFrom(config)
        transport.service.service.CopyFrom(service)
        transport.service.meta.CopyFrom(metadata)
        yield transport

def get_free_port() -> int:
    with socket.socket() as s:
        s.bind(('', 0))
        return int(s.getsockname()[1])

def longestSubstringFinder(string1, string2) -> str:
    answer = ""
    len1, len2 = len(string1), len(string2)
    for i in range(len1):
        match = ""
        for j in range(len2):
            if (i + j < len1 and string1[i + j] == string2[j]):
                match += string2[j]
            else:
                if (len(match) > len(answer)): answer = match
                match = ""
    return answer

get_only_the_ip_from_context = lambda context_peer: context_peer[5:-1*(len(context_peer.split(':')[-1])+1)] if context_peer.split(':')[0] == 'ipv4' else None  # Lleva el formato 'ipv4:49.123.106.100:4442', no queremos 'ipv4:' ni el puerto.

get_local_ip_from_network = lambda network: ni.ifaddresses(network)[ni.AF_INET][0]['addr']

def address_in_network( ip_or_uri, net) -> bool:
    #  Return if the ip network portion (addr and broadcast common) is in the ip.
    return longestSubstringFinder(
        string1=ni.ifaddresses(net)[ni.AF_INET][0]['addr'],
        string2=ni.ifaddresses(net)[ni.AF_INET][0]['broadcast']
    ) in ip_or_uri


def get_network_name( ip_or_uri: str) -> str:
    #  https://stackoverflow.com/questions/819355/how-can-i-check-if-an-ip-is-in-a-network-in-python
    for network in ni.interfaces():
        try:
            if address_in_network(ip_or_uri = ip_or_uri, net = network):
                return network
        except KeyError:
            continue

CHUNK_SIZE = 1024 * 1024  # 1MB

def get_file_chunks(filename) -> Generator[gateway_pb2.Buffer, None, None]:
    with open(filename, 'rb') as f:
        while True:
            piece = f.read(CHUNK_SIZE);
            if len(piece) == 0:
                return
            yield gateway_pb2.Buffer(chunk=piece)


def save_chunks_to_file(chunks: gateway_pb2.Buffer, filename):
    with open(filename, 'wb') as f:
        for buffer in chunks:
            f.write(buffer.chunk)

def parse_from_buffer(request_iterator, message_field = None):
    while True:
        all_buffer = bytes('', encoding='utf-8')
        while True:
            buffer = next(request_iterator)
            print('     buffer -> ', buffer)
            if buffer.separator:
                break
            all_buffer += buffer.chunk
        
        if message_field: 
            message = message_field()
            message.ParseFromString(
                all_buffer
            )
            print('     message -> ', message)
            yield message
        else:
            yield all_buffer # Clean buffer index bytes.

def serialize_to_buffer(message_iterator):
    if not hasattr(message_iterator, '__iter__'): message_iterator=[message_iterator]
    for message in message_iterator:
        byte_list = list(message.SerializeToString())
        for chunk in [byte_list[i:i + CHUNK_SIZE] for i in range(0, len(byte_list), CHUNK_SIZE)]:
            yield gateway_pb2.Buffer(
                chunk = bytes(chunk)
            )
        yield gateway_pb2.Buffer(
            separator = bytes('', encoding='utf-8')
        )

def client_grpc(method, output_field = None, input=None, timeout=None):
    for b in parse_from_buffer(
        request_iterator = method(
                            serialize_to_buffer(
                                input if input else ''
                            ),
                            timeout = timeout
                        ),
        message_field = output_field
    ): yield b