# GrpcBigBuffer.
CHUNK_SIZE = 1024 * 1024  # 1MB
import os, shutil, gc, itertools
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

def save_chunks_to_file(buffer_iterator, filename, signal):
    signal.wait()
    with open(filename, 'wb') as f:
        signal.wait()
        f.write(''.join([buffer.chunk for buffer in buffer_iterator]))

def parse_from_buffer(
        request_iterator,
        message_field_or_route = None, 
        signal = Signal(exist=False), 
        indices: dict = None,
        partitions: dict = None,
        cache_dir: str = os.path.abspath(os.curdir) + '/__hycache__/grpcbigbuffer' + str(randint(1,999)) + '/',
    ): # indice: method
    def parser_iterator(request_iterator, signal: Signal) -> Generator[bytes, None, None]:
        while True:
            signal.wait()
            buffer = next(request_iterator)
            if buffer.HasField('chunk'):
                yield buffer.chunk
            if buffer.HasField('signal') and buffer.signal:
                signal.change()
            if buffer.HasField('separator') and buffer.separator: 
                break

    def parse_message(message_field, request_iterator, signal):
        all_buffer = bytes()
        for b in parser_iterator(
            request_iterator=request_iterator,
            signal=signal,
        ):
            all_buffer += b
        message = message_field()
        message.ParseFromString(
            all_buffer
        )
        return message

    def save_to_file(filename: str, request_iterator, signal) -> str:
        save_chunks_to_file(
            filename = filename,
            buffer_iterator = parser_iterator(
                request_iterator = request_iterator,
                signal = signal,
            ),
            signal = signal,
        )
        return filename
    
    def iterate_partition(message_field_or_route, signal: Signal, request_iterator, filename: str):
        if message_field_or_route and type(message_field_or_route) is not str:
            yield parse_message(
                message_field = message_field_or_route,
                request_iterator = request_iterator,
                signal=signal,
            )

        elif message_field_or_route:
            yield save_to_file(
                request_iterator = request_iterator,
                filename = filename,
                signal = signal
            )

        else: 
            for b in parser_iterator(
                request_iterator = request_iterator,
                signal = signal
            ): yield b
    
    def iterate_partitions(partitions: list, signal: Signal, request_iterator, cache_dir: str):
        for i, partition in enumerate(partitions):
            for b in iterate_partition(
                    message_field_or_route = partition, 
                    signal = signal,
                    request_iterator = request_iterator,
                    filename = cache_dir + 'p'+str(i),
                ): yield b

    if indices and len(indices) == 1: message_field_or_route = list(indices.values())[0]
    while True:
        buffer = next(request_iterator)
        # The order of conditions is important.
        if buffer.HasField('head'):
            try:
                if buffer.head in partitions:
                    for b in iterate_partitions(
                        partitions = partitions[buffer.head],
                        signal = signal,
                        request_iterator = itertools.chain(buffer, request_iterator),
                        cache_dir = cache_dir,
                    ): yield b
                else:
                    yield parse_message(
                        message_field = indices[buffer.head],
                        request_iterator = itertools.chain(buffer, request_iterator),
                        signal = signal,
                    )
            except: pass
            
        elif partitions and len(partitions) == 1:
            for b in iterate_partitions(
                partitions = list(partitions.values())[0],
                signal = signal,
                request_iterator = itertools.chain(buffer, request_iterator),
                cache_dir = cache_dir,
            ): yield b

        else:
            for b in iterate_partition(
                message_field_or_route = message_field_or_route,
                signal = signal,
                request_iterator = itertools.chain(buffer, request_iterator),
                filename = cache_dir + 'p1',
            ): yield b

def serialize_to_buffer(
        message_iterator, 
        signal = Signal(exist=False), 
        cache_dir: str = os.path.abspath(os.curdir) + '/__hycache__/grpcbigbuffer' + str(randint(1,999)) + '/', 
        indices: dict = None, 
        mem_manager = lambda len: None
    ) -> Generator[gateway_pb2.Buffer, None, None]:  # method: indice
    def send_file(filename: str, signal: Signal) -> Generator[gateway_pb2.Buffer, None, None]:
        for b in get_file_chunks(
                filename=filename, 
                signal=signal
            ):
                signal.wait()
                try:
                    yield b
                finally: signal.wait()
        yield gateway_pb2.Buffer(
            separator = True
        )

    def send_message(
            signal: Signal, 
            message: object, 
            head: int = None, 
            mem_manager = lambda len: None,
            cache_dir: str= None, 
        ) -> Generator[gateway_pb2.Buffer, None, None]:
        message_bytes = message.SerializeToString()
        if len(message_bytes) < CHUNK_SIZE:
            signal.wait()
            try:
                yield gateway_pb2.Buffer(
                    chunk = bytes(message_bytes),
                    head = head,
                    separator = True
                ) if head else gateway_pb2.Buffer(
                        chunk = bytes(message_bytes),
                        separator = True
                    )
            finally: signal.wait()

        else:
            try:
                if head: yield gateway_pb2.Buffer(
                    head = head
                )
            finally: signal.wait()

            try:
                signal.wait()
                file = cache_dir + str(len(message_bytes))
                with open(file, 'wb') as f, mem_manager(len=os.path.getsize(file)):
                    f.write(message_bytes)
                send_file(
                    filename=file,
                    signal=signal
                )
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

    if indices: indices = {e[1]:e[0] for e in indices.items()}
    if not hasattr(message_iterator, '__iter__') or type(message_iterator) is tuple: message_iterator=[message_iterator]
    for message in message_iterator:
        if type(message) is tuple:  # If is partitioned
            try:
                yield gateway_pb2.Buffer(
                    head = indices[message[0]]
                )
            except:
                yield gateway_pb2.Buffer(head = 1)
            
            for partition in message[1:]:
                if type(partition) is str:
                    for b in send_file(
                        filename = message[1],
                        signal=signal
                    ): yield b
                else:
                    for b in send_message(
                        signal=signal,
                        message=partition,
                        mem_manager=mem_manager,
                        cache_dir = cache_dir,
                    ): yield b

        else:  # If message is a protobuf object.
            try:
                    head = indices[type(message)]
            except:  # if not indices or the method not appear on it.
                    head = 1
            for b in send_message(
                signal=signal,
                message=message,
                head=head,
                mem_manager=mem_manager,
                cache_dir = cache_dir,
            ): yield b

def client_grpc(method, output_field_or_route = None, input=None, timeout=None, indices_parser: dict = None, indices_serializer: dict = None, mem_manager = lambda len: None): # indice: method
    signal = Signal()
    cache_dir = os.path.abspath(os.curdir) + '/__hycache__/grpcbigbuffer' + str(randint(1,999)) + '/'
    os.mkdir(cache_dir)
    try:
        for b in parse_from_buffer(
            request_iterator = method(
                                serialize_to_buffer(
                                    message_iterator = input if input else '',
                                    signal = signal,
                                    cache_dir = cache_dir,
                                    indices = indices_serializer,
                                    mem_manager=mem_manager
                                ),
                                timeout = timeout
                            ),
            message_field_or_route = output_field_or_route,
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