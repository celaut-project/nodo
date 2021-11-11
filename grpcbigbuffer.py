__version__ = 'dev'

# GrpcBigBuffer.
CHUNK_SIZE = 1024 * 1024  # 1MB
import os, shutil, gc, itertools
import buffer_pb2
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

def get_file_chunks(filename, signal = Signal(exist=False)) -> Generator[buffer_pb2.Buffer, None, None]:
    signal.wait()
    try:
        with open(filename, 'rb', buffering = CHUNK_SIZE) as f:
            while True:
                f.flush()
                signal.wait()
                piece = f.read(CHUNK_SIZE)
                if len(piece) == 0: return
                yield buffer_pb2.Buffer(chunk=piece)
    finally: 
        gc.collect()

def save_chunks_to_file(buffer_iterator, filename, signal):
    signal.wait()
    with open(filename, 'wb') as f:
        signal.wait()
        f.write(''.join([buffer.chunk for buffer in buffer_iterator]))

def parse_from_buffer(
        request_iterator, 
        signal = Signal(exist=False), 
        message_field = None,
        indices: dict = {}, # indice: method
        partitions_model: dict = {},
        partitions_message_mode: dict = {},
        cache_dir: str = os.path.abspath(os.curdir) + '/__hycache__/grpcbigbuffer' + str(randint(1,999)) + '/',
        mem_manager = lambda len: None,
        yield_remote_partition_dir: bool = False,
    ): 
    if indices == {} and message_field: indices = {1: message_field}
    os.mkdir(cache_dir)

    def parser_iterator(request_iterator, signal: Signal) -> Generator[bytes, None, None]:
        while True:
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
    
    def iterate_partitions(signal: Signal, request_iterator, cache_dir: str, partitions: list = [None]):
        for i, partition in enumerate(partitions):
            for b in iterate_partition(
                    message_field_or_route = partition if partition and type(partition) is not str else '', 
                    signal = signal,
                    request_iterator = request_iterator,
                    filename = cache_dir + 'p'+str(i+1),
                ): yield b

    def conversor(
            iterator,
            indices: dict,
            signal: Signal, 
            pf_object: object = None, 
            local_partitions_model: list = [], 
            remote_partitions_model: list = [], 
            mem_manager = lambda len: None, 
            yield_remote_partition_dir: bool = False, 
            cache_dir: str = None,
            partitions_message_mode: dict = {},
        ):
        dirs = []
        # 1. Save the remote partitions on cache.
        for d in iterator: 
            # 2. yield remote partitions directory.
            if yield_remote_partition_dir: yield d
            dirs.append(d)

        if not pf_object or len(dirs) != len(remote_partitions_model): return None
        # 3. Parse to the local partitions from the remote partitions using mem_manager.
        with mem_manager(len = 2*sum([os.path.getsize(dir) for dir in dirs])):
            main_object = pf_object()
            for i, d in enumerate(dirs):
                # Get the partition
                partition = remote_partitions_model[i]
                
                # Get auxiliar object for partition.
                def recursive(partition, aux_object):
                    return recursive(
                        aux_object = eval(aux_object.DESCRIPTOR.fields_by_number[list(partition.index.keys())[0]].message_type.full_name), 
                        partition = list(partition.index.values())[0]
                        ) if partition.HasField('index') and len(partition.index) == 1 else aux_object() 
                aux_object = recursive(partition = partition, aux_object = pf_object)

                # Parse buffer to it.
                try:
                    aux_object.ParseFromString(open(d, 'rb').read())
                except: return None

                main_object.MergeFrom(aux_object)

        # 4. yield local partitions.
        for b in parse_from_buffer(
            request_iterator = serialize_to_buffer(
                                    signal = signal,
                                    cache_dir = cache_dir,
                                    partitions_model = local_partitions_model,
                                    mem_manager = mem_manager,
                                    indices = indices,
                                ),
            signal = signal,
            indices = indices,
            cache_dir = cache_dir,
            mem_manager = mem_manager,
            partitions_model = local_partitions_model,
            partitions_message_mode = partitions_message_mode,
        ): yield b


    try: 
        while True:
            buffer = next(request_iterator)
            # The order of conditions is important.
            if buffer.HasField('head'):
                try:
                    # If not match
                    if len(buffer.head.partitions)==0 and len(buffer.head.partitions) > 1 and \
                        buffer.head.index in partitions_model and partitions_model[buffer.head.index] and \
                        buffer.head.partitions != partitions_model[buffer.head.index] \
                        or len(buffer.head.partitions)==0 and len(buffer.head.partitions) > 1 and \
                            not (buffer.head.index in partitions_model and partitions_model[buffer.head.index]) \
                        or not (len(buffer.head.partitions)==0 and len(buffer.head.partitions) > 1) and \
                            buffer.head.index in partitions_model and partitions_model[buffer.head.index]:
                        for b in conversor(
                            iterator = iterate_partitions(
                                partitions = [None for i in buffer.head.partitions] if len(buffer.head.partitions)==0 else [None],
                                signal = signal,
                                request_iterator = itertools.chain([buffer], request_iterator),
                                cache_dir = cache_dir + 'remote/'
                            ),
                            indices = indices,
                            cache_dir = cache_dir,
                            local_partitions_model = partitions_model[buffer.head.index] if buffer.head.index in partitions_model else [None],
                            remote_partitions_model = buffer.head.partitions,
                            mem_manager = mem_manager,
                            yield_remote_partition_dir = yield_remote_partition_dir,
                            pf_object = indices[buffer.head.index] if buffer.head.index in indices else None,
                            partitions_message_mode = partitions_message_mode,
                        ): yield b

                    elif buffer.head.index in partitions_model and partitions_model[buffer.head.index] and len(partitions_model[buffer.head.index]) > 1:
                        yield indices[buffer.head.index]()
                        for b in iterate_partitions(
                            partitions = partitions_message_mode[buffer.head.index] if buffer.head.index in partitions_message_mode else [None for p in partitions_model[buffer.head.index]],
                            signal = signal,
                            request_iterator = itertools.chain([buffer], request_iterator),
                            cache_dir = cache_dir,
                        ): yield b
                    else:
                        for b in iterate_partition(
                            message_field_or_route = partitions_message_mode[buffer.head.index][0] \
                                if buffer.head.index in partitions_message_mode and len(partitions_message_mode[buffer.head.index]) > 0 else indices[buffer.head.index],
                            signal = signal,
                            request_iterator = itertools.chain([buffer], request_iterator),
                            filename = cache_dir + 'p1',
                        ): yield b
                except: pass

            elif indices and len(indices) == 1: # Does not've more than one index and more than one partition too.
                if partitions_message_mode and 1 in partitions_message_mode and len(partitions_message_mode[1]) > 1:
                    yield list(indices.values())[0]
                    for b in conversor(
                        iterator = iterate_partition(
                            message_field_or_route = '',
                            signal = signal,
                            request_iterator = itertools.chain([buffer], request_iterator),
                            filename = cache_dir + 'remote/p1',
                        ),
                        indices = indices,
                        remote_partitions_model = [None],
                        local_partitions_model = list(partitions_model.values())[0] if list(partitions_model.values()) > 0 else [None],
                        mem_manager = mem_manager,
                        yield_remote_partition_dir = yield_remote_partition_dir,
                        pf_object = list(indices.values())[0],
                        cache_dir = cache_dir,
                        partitions_message_mode = partitions_message_mode,
                    ): yield b
                else:
                    for b in iterate_partition(
                        message_field_or_route = list(partitions_message_mode.values())[0][0] if len(partitions_message_mode) == 1 and len(list(partitions_message_mode.values())[0]) > 0 else list(indices.values())[0],
                        signal = signal,
                        request_iterator = itertools.chain([buffer], request_iterator),
                        filename = cache_dir + 'p1',
                    ): yield b
            else:
                raise Exception('Failed parsing. Comunication error.')
    except StopIteration:
        try:
            shutil.rmtree(cache_dir)
        except: pass
        return

def serialize_to_buffer(
        message_iterator,
        signal = Signal(exist=False),
        cache_dir: str = os.path.abspath(os.curdir) + '/__hycache__/grpcbigbuffer' + str(randint(1,999)) + '/', 
        indices: dict = {}, 
        partitions_model: dict = {},
        mem_manager = lambda len: None
    ) -> Generator[buffer_pb2.Buffer, None, None]:  # method: indice
    
    os.mkdir(cache_dir)
    if indices: indices = {e[1]:e[0] for e in indices.items()}
    if not hasattr(message_iterator, '__iter__') or type(message_iterator) is tuple: message_iterator=[message_iterator]

    def send_file(filename: str, signal: Signal) -> Generator[buffer_pb2.Buffer, None, None]:
        for b in get_file_chunks(
                filename=filename, 
                signal=signal
            ):
                signal.wait()
                try:
                    yield b
                finally: signal.wait()
        yield buffer_pb2.Buffer(
            separator = True
        )

    def send_message(
            signal: Signal, 
            message: object, 
            head: buffer_pb2.Buffer.Head = None, 
            mem_manager = lambda len: None,
            cache_dir: str= None, 
        ) -> Generator[buffer_pb2.Buffer, None, None]:
        message_bytes = message.SerializeToString()
        if len(message_bytes) < CHUNK_SIZE:
            signal.wait()
            try:
                yield buffer_pb2.Buffer(
                    chunk = bytes(message_bytes),
                    head = head,
                    separator = True
                ) if head else buffer_pb2.Buffer(
                        chunk = bytes(message_bytes),
                        separator = True
                    )
            finally: signal.wait()

        else:
            try:
                if head: yield buffer_pb2.Buffer(
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
                yield buffer_pb2.Buffer(
                    separator = True
                )
            finally: signal.wait()

    for message in message_iterator:
        if type(message) is tuple:  # If is partitioned
            try:
                yield buffer_pb2.Buffer(
                    head = buffer_pb2.Buffer.Head(
                        index = indices[message[0]],
                        partitions = partitions_model[indices[message[0]]]
                    )
                )
            except:
                yield buffer_pb2.Buffer(
                    head = buffer_pb2.Buffer.Head(
                        index = 1,
                        partitions = partitions_model[1]
                    )
                )
            
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
                    head = buffer_pb2.Buffer.Head(
                        index = indices[type(message)],
                        partitions = partitions_model[indices[type(message)]] if indices[type(message)] in partitions_model else None
                    )
            except:  # if not indices or the method not appear on it.
                    head = buffer_pb2.Buffer.Head(
                        index = 1,
                        partitions = partitions_model[1] if 1 in partitions_model else None
                    )
            for b in send_message(
                signal=signal,
                message=message,
                head=head,
                mem_manager=mem_manager,
                cache_dir = cache_dir,
            ): yield b
    try:
        shutil.rmtree(cache_dir)
    except: pass
    return

def client_grpc(
        method,
        input = None, 
        output_field = None,
        timeout = None, 
        indices_parser: dict = {}, 
        partitions_parser: dict = {}, 
        partitions_message_mode_parser: dict = {}, 
        indices_serializer: dict = {}, 
        partitions_serializer: dict = {}, 
        mem_manager = lambda len: None,
        yield_remote_partition_dir_on_serializer: bool = False,
    ): # indice: method
    signal = Signal()
    for b in parse_from_buffer(
        request_iterator = method(
                            serialize_to_buffer(
                                message_iterator = input if input else '',
                                signal = signal,
                                indices = indices_serializer,
                                partitions_model = partitions_serializer,
                                mem_manager = mem_manager,
                            ),
                            timeout = timeout
                        ),
        signal = signal,
        indices = indices_parser,
        message_field = output_field,
        partitions_model = partitions_parser,
        partitions_message_mode = partitions_message_mode_parser if partitions_message_mode_parser != {} else {1:[output_field]},
        yield_remote_partition_dir = yield_remote_partition_dir_on_serializer,
    ): yield b

"""
    Serialize Object to plain bytes serialization.
"""
def serialize_to_plain(object: object) -> bytes:
    pass