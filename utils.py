from posix import sched_param
import socket
from typing import Generator, final

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
        service_buffer: bytes,
        metadata: celaut_pb2.Any.Metadata,  
        config: celaut_pb2.Configuration = None
    ) -> Generator[object, None, None]:

        set_config = True if config else False
        for hash in metadata.hash:
            if set_config:  # Solo hace falta enviar la configuracion en el primer paquete.
                set_config = False
                yield gateway_pb2.HashWithConfig(
                    hash = hash,
                    config = celaut_pb2.Configuration()
                )
            yield hash

        any = celaut_pb2.Any(
                metadata = metadata,
                value = service_buffer
            )
        if set_config: 
            yield gateway_pb2.ServiceWithConfig(
                    service = any,
                    config = celaut_pb2.Configuration()
                )
        yield any

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

# GrpcBigBuffer.
CHUNK_SIZE = 1024 * 1024  # 1MB
import os, shutil, gc
import gateway_pb2
from random import randint
from typing import Generator
from threading import Condition

class Signal():
    # The parser use change() when reads a signal on the buffer.
    # The serializer use wait() for stop to send the buffer if it've to do it.
    # It's thread safe because the open var is only used by one thread (the parser) with the change method.
    def __init__(self, exist: bool = True) -> None:
        self.exist = exist
        if exist: self.open = True
        if exist: self.condition = Condition()
    
    def change(self):
        if self.exist:
            if self.open:
                self.open = False  # Stop the input buffer.
            else:
                with self.condition:
                    self.condition.notify_all()
                self.open = True  # Continue the input buffer.
    
    def wait(self):
        if self.exist and not self.open:
            with self.condition:
                self.condition.wait()

def get_file_chunks(filename, signal = Signal(exist=False)) -> Generator[gateway_pb2.Buffer, None, None]:
    signal.wait()
    with open(filename, 'rb') as f:
        while True:
            signal.wait()
            piece = f.read(CHUNK_SIZE);
            if len(piece) == 0:
                return
            try:
                yield gateway_pb2.Buffer(chunk=piece)
            finally: signal.wait()


def save_chunks_to_file(buffer_iterator, filename):
    with open(filename, 'wb') as f:
        for buffer in buffer_iterator:
            f.write(buffer.chunk)

def parse_from_buffer(request_iterator, message_field = None, signal = Signal(exist=False), indices: dict = None): # indice: method
    if indices and len(indices) == 1: message_field = list(indices.values())[0]
    while True:
        all_buffer = bytes()
        while True:
            buffer = next(request_iterator)
            # The order of conditions is important.
            print('buffer -> ', buffer.head, len(buffer.chunk), buffer.signal, buffer.separator)
            if buffer.HasField('head'):
                try:
                    message_field = indices[buffer.head]
                except: pass
            if buffer.HasField('chunk'):
                all_buffer += buffer.chunk
            if buffer.HasField('signal') and buffer.signal:
                print('is signal')
                signal.change()
                continue
            if buffer.HasField('separator') and buffer.separator: 
                print('is separator')
                break
        print('message field', message_field, indices.keys())
        if message_field:
            message = message_field()
            message.ParseFromString(
                all_buffer
            )
            yield message
        else:
            yield all_buffer

def serialize_to_buffer(message_iterator, signal = Signal(exist=False), cache_dir = None, indices: dict = None): # method: indice
    if not hasattr(message_iterator, '__iter__'): message_iterator=[message_iterator]
    for message in message_iterator:
        try:
                head = indices[type(message)]
        except:  # if not indices or the method not appear on it.
                head = 1

        message_bytes = message.SerializeToString()
        if len(message_bytes) < CHUNK_SIZE:
            signal.wait()
            try:
                yield gateway_pb2.Buffer(
                    chunk = bytes(message_bytes),
                    head = head,
                    separator = True
                )
            finally: signal.wait()

        else:
            try:
                yield gateway_pb2.Buffer(
                    head = head
                )
            finally: signal.wait()

            try:
                signal.wait()
                print('vamos a pasar todo a lista ', len(message_bytes))
                byte_list = list(message_bytes)
                for chunk in [byte_list[i:i + CHUNK_SIZE] for i in range(0, len(byte_list), CHUNK_SIZE)]:
                    signal.wait()
                    try:
                        yield gateway_pb2.Buffer(
                                        chunk = bytes(chunk)
                                    )
                    finally: signal.wait()
            except:    
                try:
                    signal.wait()
                    print('vamos a escribir en cache ', len(message_bytes))
                    file = cache_dir + str(len(message_bytes))
                    open(file, 'wb').write(message_bytes)
                    for b in get_file_chunks(file, signal=signal): 
                        signal.wait()
                        try:
                            yield b
                        finally: signal.wait()
                finally:
                    try:
                        os.remove(file)
                        gc.collect()
                    except: pass

            try:
                yield gateway_pb2.Buffer(
                    separator = True
                )
            finally: signal.wait()

def client_grpc(method, output_field = None, input=None, timeout=None, indices_parser: dict = None, indices_serializer: dict = None): # indice: method
    signal = Signal()
    cache_dir = os.path.abspath(os.curdir) + '/__hycache__/grpcbigbuffer' + str(randint(1,999)) + '/'
    os.mkdir(cache_dir)
    try:
        for b in parse_from_buffer(
            request_iterator = method(
                                serialize_to_buffer(
                                    input if input else '',
                                    signal = signal,
                                    cache_dir = cache_dir,
                                    indices = {e[1]:e[0] for e in indices_serializer.items()} if indices_serializer else None
                                ),
                                timeout = timeout
                            ),
            message_field = output_field,
            signal = signal,
            indices = indices_parser
        ): yield b
    finally:
        try:
            shutil.rmtree(cache_dir)
            gc.collect()
        except: pass


"""
    Serialize Object to plain bytes serialization.
"""
def serialize_to_plain(object: object) -> bytes:
    pass