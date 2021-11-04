import socket, psutil
from time import sleep
from typing import Generator
from threading import Lock

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

# I/O Big Data utils.
import psutil, os.path, gc
from time import sleep
from threading import Lock
from singleton import Singleton

def read_file(filename) -> bytes:
    def generator(filename):
        with open(filename, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                    yield chunk

    print('go to read it.')
    return b''.join([b for b in generator(filename)])

class IOBigData(metaclass=Singleton):

    class RamLocker(object):
        def __init__(self, len, iobd):
            self.len = len
            self.iobd = iobd

        def __enter__(self):
            self.iobd.lock_ram(ram_amount = self.len)
            return self

        def unlock(self, amount: int):
            self.iobd.unlock_ram(ram_amount = amount)
            self.len -= amount

        def __exit__(self, type, value, traceback):
            self.iobd.unlock_ram(ram_amount = self.len)
            gc.collect()

    def __init__(self) -> None:
        print('Init object')
        self.ram_pool = psutil.virtual_memory().available
        self.ram_locked = 0
        self.get_ram_avaliable = lambda: self.ram_pool - self.ram_locked
        self.amount_lock = Lock()

    def stats(self):
        with self.amount_lock:
            print('----------------------')
            print('RAM LOCKED -> ', self.ram_locked)
            print('RAM AVALIABLE -> ', self.get_ram_avaliable())
            print('----------------------')

    def lock(self, len):
        return self.RamLocker(len = len, iobd = self)

    def lock_ram(self, ram_amount: int, wait: bool = True):
        self.stats()
        if wait:
            self.wait_to_prevent_kill(len = ram_amount)
        elif not self.prevent_kill(len = ram_amount):
            raise Exception
        with self.amount_lock:
            self.ram_locked += ram_amount
        self.stats()

    def unlock_ram(self, ram_amount: int):
        with self.amount_lock:
            if ram_amount < self.ram_locked:
                self.ram_locked -= ram_amount
            else:
                self.ram_locked = 0
        self.stats()

    def prevent_kill(self, len: int) -> bool:
        with self.amount_lock:
            b = self.get_ram_avaliable() > len
        return b

    def wait_to_prevent_kill(self, len: int) -> None:
        while True:
            if not self.prevent_kill(len = len):
                sleep(1)
            else:
                return




# GrpcBigBuffer.
CHUNK_SIZE = 1024 * 1024  # 1MB
import os, shutil, gc
import gateway_pb2
from random import randint, random
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
    try:
        with open(filename, 'rb', buffering = CHUNK_SIZE) as f:
            while True:
                f.flush()
                signal.wait()
                piece = f.read(CHUNK_SIZE)
                if len(piece) == 0: return
                yield gateway_pb2.Buffer(chunk=piece)
    finally: 
        gc.collect()

def save_chunks_to_file(buffer_iterator, filename):
    with open(filename, 'wb') as f:
        f.write(''.join([buffer.chunk for buffer in buffer_iterator]))

def parse_from_buffer(request_iterator, message_field = None, signal = Signal(exist=False), indices: dict = None): # indice: method
    if indices and len(indices) == 1: message_field = list(indices.values())[0]
    while True:
        all_buffer = bytes()
        while True:
            buffer = next(request_iterator)
            # The order of conditions is important.
            if buffer.HasField('head'):
                try:
                    message_field = indices[buffer.head]
                except: pass
            if buffer.HasField('chunk'):
                all_buffer += buffer.chunk
            if buffer.HasField('signal') and buffer.signal:
                signal.change()
                continue
            if buffer.HasField('separator') and buffer.separator: 
                break
        if message_field:
            message = message_field()
            message.ParseFromString(
                all_buffer
            )
            yield message
        else:
            yield all_buffer

def serialize_to_buffer(message_iterator, signal = Signal(exist=False), cache_dir = None, indices: dict = None): # method: indice
    if not hasattr(message_iterator, '__iter__') or type(message_iterator) is tuple: message_iterator=[message_iterator]
    for message in message_iterator:
        if type(message) is tuple:
            try:
                yield gateway_pb2.Buffer(
                    head = indices[message[1]]
                )
            except:
                yield gateway_pb2.Buffer(head = 1)
            
            for b in get_file_chunks(filename = message[0], signal = signal):
                yield b
            
            yield gateway_pb2.Buffer(separator = True)

        else: # if message is a protobuf object.
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
                    file = cache_dir + str(len(message_bytes))
                    with open(file, 'wb') as f:
                        f.write(message_bytes)
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