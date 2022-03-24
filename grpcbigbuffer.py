__version__ = 'dev'

# GrpcBigBuffer.
CHUNK_SIZE = 1024 * 1024  # 1MB
MAX_DIR = 999999999
import os, gc, itertools, sys

from google import protobuf
import buffer_pb2
from random import randint
from typing import Generator, Union, final
from threading import Condition

class EmptyBufferException(Exception):
    pass

class Dir(object):
    def __init__(self, dir: str):
        self.name = dir

class MemManager(object):
    def __init__(self, len):
        pass
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_value, trace):
        pass
    
class Enviroment(type):
    # Using singleton pattern
    _instances = {}
    cache_dir = os.path.abspath(os.curdir) + '/__cache__/grpcbigbuffer/'
    mem_manager = lambda len: MemManager(len=len)

    def __call__(cls):
        if cls not in cls._instances:
            cls._instances[cls] = super(Enviroment, cls).__call__()
        return cls._instances[cls]

def modify_env(cache_dir: str = None, mem_manager = None):
    if cache_dir: Enviroment.cache_dir = cache_dir + 'grpcbigbuffer/'
    if mem_manager: Enviroment.mem_manager = mem_manager

def message_to_bytes(message) -> bytes:
    if type(type(message)) is protobuf.pyext.cpp_message.GeneratedProtocolMessageType:
        return message.SerializeToString()
    elif type(message) is str:
        return bytes(message, 'utf-8')
    else:
        try:
            return bytes(message)
        except TypeError: raise('gRPCbb error -> Serialize message error: some primitive type message not suported for contain partition '+ str(type(message)))

def generate_random_dir() -> str: 
    cache_dir = Enviroment.cache_dir
    try:
        os.mkdir(cache_dir)
    except FileExistsError: pass
    while True:
        file = cache_dir+str(randint(1, MAX_DIR))
        if not os.path.isfile(file): return file

def remove_file(file: str):
    os.remove(file) # TODO could be async.

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

def get_file_chunks(filename, signal = None) -> Generator[buffer_pb2.Buffer, None, None]:
    if not signal: signal = Signal(exist=False)
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

def save_chunks_to_file(buffer_iterator, filename, signal = None):
    if not signal: signal = Signal(exist=False)
    signal.wait()
    with open(filename, 'wb') as f:
        signal.wait()
        for buffer in buffer_iterator: f.write(buffer.chunk) # MAYBE IT'S MORE SLOW, BUT USES ONLY THE CHUNK LEN OF RAM.
        # f.write(b''.join([buffer.chunk for buffer in buffer_iterator])) # MAYBE IT'S FASTER BUT CONSUMES A LOT OF RAM with big buffers.

def get_subclass(partition, object_cls):
    return get_subclass(
        object_cls = type(
                            getattr(
                                object_cls(),
                                object_cls.DESCRIPTOR.fields_by_number[list(partition.index.keys())[0]].name
                            )
                        ), 
        partition = list(partition.index.values())[0]
        ) if len(partition.index) == 1 else object_cls

def copy_message(obj, field_name, message):
    e = getattr(obj, field_name) if field_name else obj
    if hasattr(message, 'CopyFrom'):
        e.CopyFrom(message)
    elif type(message) is bytes:
        e.ParseFromString(message)
    else:
        e = message
    return obj

def get_submessage(partition, obj, say_if_not_change = False):
    if len(partition.index) == 0:
        return False if say_if_not_change else obj
    if len(partition.index) == 1:
        return get_submessage(
            partition = list(partition.index.values())[0],
            obj = getattr(obj, obj.DESCRIPTOR.fields[list(partition.index.keys())[0]-1].name)
        )
    for field in obj.DESCRIPTOR.fields:
        if field.index+1 in partition.index:
            try:
                submessage = get_submessage(
                        partition = partition.index[field.index+1],
                        obj = getattr(obj, field.name),
                        say_if_not_change = True
                    )
                if not submessage: continue  # Anything to prune.
                copy_message(
                    obj=obj, field_name=field.name,
                    message = submessage
                )
            except: pass
        else:
            obj.ClearField(field.name)
    return obj

def put_submessage(partition, message, obj):
    if len(partition.index) == 0:
        return copy_message(
            obj=obj, field_name=None,
            message=message
        )
    if len(partition.index) == 1:
        p = list(partition.index.values())[0]
        if len(p.index) == 1:
            field_name = obj.DESCRIPTOR.fields[list(partition.index.keys())[0]-1].name
            return copy_message(
                obj=obj, field_name=field_name,
                message = put_submessage(
                            partition = p,
                            obj = getattr(obj, field_name),
                            message = message,
                        )     
            )
        else:
            return copy_message(
                obj=obj, field_name=obj.DESCRIPTOR.fields[list(partition.index.keys())[0]-1].name,
                message=message
            )

def combine_partitions(
    obj_cls: protobuf.pyext.cpp_message.GeneratedProtocolMessageType,
    partitions_model: tuple,
    partitions: tuple
    ):
    obj = obj_cls()
    for i, partition in enumerate(partitions):
        if type(partition) is str:
            with open(partition, 'rb') as f:
                partition = f.read()
        elif not (hasattr(partition, 'SerializeToString') or not type(partition) is bytes): # TODO check.   'not type(partition) is bytes' could affect on partitions to buffer()
            raise Exception('Partitions to buffer error.')
        obj = put_submessage(
            partition = partitions_model[i],
            message = partition,
            obj = obj
        )
    return obj

def parse_from_buffer(
        request_iterator, 
        signal: Signal = None, 
        indices: Union[protobuf.pyext.cpp_message.GeneratedProtocolMessageType, dict] = None, # indice: method      message_field = None,
        partitions_model: Union[list, dict] = None,
        partitions_message_mode: Union[bool, list, dict] = False,  # Write on disk by default.
        mem_manager = None,
        yield_remote_partition_dir: bool = False,
    ): 
    try:
        if not indices: indices = buffer_pb2.Empty()
        if not partitions_model: partitions_model = [buffer_pb2.Buffer.Head.Partition()]
        if not signal: signal = Signal(exist=False)
        if not mem_manager: mem_manager = Enviroment.mem_manager
        if type(indices) is protobuf.pyext.cpp_message.GeneratedProtocolMessageType: indices = {1: indices}
        if type(indices) is not dict: raise Exception

        if type(partitions_model) is list: partitions_model = {1: partitions_model}  # Only've one index.
        if type(partitions_model) is not dict: raise Exception
        for i in indices.keys():
            if i in partitions_model:
                if type(partitions_model[i]) is buffer_pb2.Buffer.Head.Partition: partitions_model[i] = [partitions_model[i]]
                if type(partitions_model[i]) is not list: raise Exception
            else:
                partitions_model.update({i: [buffer_pb2.Buffer.Head.Partition()]})

        if type(partitions_message_mode) is bool:
            partitions_message_mode = {i: [partitions_message_mode for m in l] for i, l in partitions_model.items()} # The same mode for all index and partitions.
        if type(partitions_message_mode) is list: partitions_message_mode = {1: partitions_message_mode} # Only've one index.
        for i, l in partitions_message_mode.items():  # If an index in the partitions message mode have a boolean, it applies for all partitions of this index.
            if type(l) is bool: partitions_message_mode[i] = [l for m in partitions_model[i]]
            elif type(l) is not list: raise Exception
        partitions_message_mode.update({i: [False] for i in indices if i not in partitions_message_mode})  # Check that it've all indices.
        
        if partitions_message_mode.keys() != indices.keys(): raise Exception # Check that partition modes' index're correct.
        for i in indices.keys(): # Check if partitions modes and partitions have the same lenght in all indices.
            if len(partitions_message_mode[i]) != len(partitions_model[i]):
                raise Exception
    except:
        raise Exception('Parse from buffer error: Partitions or Indices are not correct.' + str(partitions_model) + str(partitions_message_mode) + str(indices))

    def parser_iterator(request_iterator, signal: Signal = None) -> Generator[buffer_pb2.Buffer, None, None]:
        if not signal: signal = Signal(exist=False)
        while True:
            try:
                buffer = next(request_iterator)
            except StopIteration: raise Exception('AbortedIteration')

            if buffer.HasField('signal') and buffer.signal:
                signal.change()
            if buffer.HasField('chunk'):
                yield buffer
            elif not buffer.HasField('head'): break
            if buffer.HasField('separator') and buffer.separator:
                break

    def parse_message(message_field, request_iterator, signal):
        all_buffer = None
        for b in parser_iterator(
            request_iterator=request_iterator,
            signal=signal,
        ):
            if not all_buffer: all_buffer = b.chunk
            else: all_buffer += b.chunk
        
        if all_buffer == None: raise EmptyBufferException()
        if message_field is str:
            return all_buffer.decode('utf-8')
        elif type(message_field) is protobuf.pyext.cpp_message.GeneratedProtocolMessageType:
            message = message_field()
            message.ParseFromString(
                    all_buffer
                )
            return message
        else:
            try:
                return message_field(all_buffer)
            except Exception as e:
                raise Exception('gRPCbb error -> Parse message error: some primitive type message not suported for contain partition '+ str(message_field) + str(e))

    def save_to_file(request_iterator, signal) -> str:
        filename = generate_random_dir()
        try:
            save_chunks_to_file(
                filename = filename,
                buffer_iterator = parser_iterator(
                    request_iterator = request_iterator,
                    signal = signal,
                ),
                signal = signal,
            )
            return filename
        except Exception as e:
            remove_file(file=filename)
            raise e
    
    def iterate_partition(message_field_or_route, signal: Signal, request_iterator):
        if message_field_or_route and type(message_field_or_route) is not str:
            return parse_message(
                message_field = message_field_or_route,
                request_iterator = request_iterator,
                signal=signal,
            )

        else:
            return save_to_file(
                request_iterator = request_iterator,
                signal = signal
            )
    
    def iterate_partitions(signal: Signal, request_iterator, partitions: list = None):
        if not partitions: partitions = [None]
        for i, partition in enumerate(partitions):
            try:
                yield iterate_partition(
                        message_field_or_route = partition, 
                        signal = signal,
                        request_iterator = request_iterator,
                    )
            except EmptyBufferException: continue

    def conversor(
            iterator,
            pf_object: object = None, 
            local_partitions_model: list = None, 
            remote_partitions_model: list = None, 
            mem_manager = Enviroment.mem_manager, 
            yield_remote_partition_dir: bool = False, 
            partitions_message_mode: list = None,
        ):
        if not local_partitions_model: local_partitions_model = []
        if not remote_partitions_model: remote_partitions_model = []
        if not partitions_message_mode: partitions_message_mode = []
        yield pf_object
        dirs = []
        # 1. Save the remote partitions on cache.
        try:
            for d in iterator: 
                # 2. yield remote partitions directory.
                if yield_remote_partition_dir: yield d
                dirs.append(d)
        except EmptyBufferException: pass
        if not pf_object or len(remote_partitions_model)>0 and len(dirs) != len(remote_partitions_model): return None
        # 3. Parse to the local partitions from the remote partitions using mem_manager.
        # TODO: check the limit memory formula.
        with mem_manager(len = 3*sum([os.path.getsize(dir) for dir in dirs[:-1]]) + 2*os.path.getsize(dirs[-1])):
            if (len(remote_partitions_model)==0 or len(remote_partitions_model)==1) and len(dirs)==1:
                main_object = pf_object()
                main_object.ParseFromString(open(dirs[0], 'rb').read())
                remove_file(file=dirs[0])
            elif len(remote_partitions_model)!=len(dirs): 
                raise Exception("Error: remote partitions model are not correct with the buffer.")
            else:
                main_object = combine_partitions(
                    obj_cls = pf_object,
                    partitions_model = remote_partitions_model,
                    partitions = dirs
                )
                for dir in dirs: remove_file(dir)

            # 4. yield local partitions.
            if local_partitions_model == []: local_partitions_model.append(buffer_pb2.Buffer.Head.Partition())
            for i, partition in enumerate(local_partitions_model):
                if i+1 == len(local_partitions_model): 
                    aux_object = main_object
                else:
                    aux_object = pf_object()
                    aux_object.CopyFrom(main_object)
                aux_object = get_submessage(partition = partition, obj = aux_object)
                message_mode = partitions_message_mode[i]
                if not message_mode:
                    filename = generate_random_dir()
                    with open(filename, 'wb') as f:
                        f.write(
                            aux_object.SerializeToString() if hasattr(aux_object, 'SerializeToString') \
                                else bytes(aux_object) if type(aux_object) is not str else bytes(aux_object, 'utf8')
                        )
                    del aux_object
                    yield filename
                else:
                    yield aux_object


    for buffer in request_iterator:
        # The order of conditions is important.
        if buffer.HasField('head'):
            if buffer.head.index not in indices: raise Exception('Parse from buffer error: buffer head index is not correct ' + str(buffer.head.index) + str(indices.keys()))
            if not ((len(buffer.head.partitions)==0 and len(partitions_model[buffer.head.index])==1) or \
                    (len(buffer.head.partitions) == len(partitions_model[buffer.head.index]) and
                        list(buffer.head.partitions) == partitions_model[buffer.head.index])):  # If not match
                for b in conversor(
                    iterator = iterate_partitions(
                        partitions = [None for i in buffer.head.partitions] if len(buffer.head.partitions)>0 else [None],
                        signal = signal,
                        request_iterator = itertools.chain([buffer], request_iterator),
                    ),
                    local_partitions_model = partitions_model[buffer.head.index],
                    remote_partitions_model = buffer.head.partitions,
                    mem_manager = mem_manager,
                    yield_remote_partition_dir = yield_remote_partition_dir,
                    pf_object = indices[buffer.head.index],
                    partitions_message_mode = partitions_message_mode[buffer.head.index],
                ): yield b

            elif len(partitions_model[buffer.head.index]) > 1:
                yield indices[buffer.head.index]
                for b in iterate_partitions(
                    partitions = [get_subclass(object_cls = indices[buffer.head.index], partition = partition) \
                        if partitions_message_mode[buffer.head.index][part_i] else None for part_i, partition in enumerate(partitions_model[buffer.head.index])], # TODO performance
                    signal = signal,
                    request_iterator = itertools.chain([buffer], request_iterator),
                ): yield b

            else:
                try:
                    yield iterate_partition(
                        message_field_or_route = indices[buffer.head.index] if partitions_message_mode[buffer.head.index][0] else None,
                        signal = signal,
                        request_iterator = itertools.chain([buffer], request_iterator),
                    )
                except EmptyBufferException: continue

        elif 1 in indices: # Does not've more than one index and more than one partition too.
            if len(partitions_model[1]) > 1:
                for b in conversor(
                    iterator = iterate_partitions(
                            signal = signal,
                            request_iterator = itertools.chain([buffer], request_iterator),
                        ),
                    remote_partitions_model = [buffer_pb2.Buffer.Head.Partition()],
                    local_partitions_model = partitions_model[1],
                    mem_manager = mem_manager,
                    yield_remote_partition_dir = yield_remote_partition_dir,
                    pf_object = indices[1],
                    partitions_message_mode = partitions_message_mode[1],
                ): yield b
            else:
                try:
                    yield iterate_partition(
                        message_field_or_route = indices[1] if partitions_message_mode[1][0] else None,
                        signal = signal,
                        request_iterator = itertools.chain([buffer], request_iterator),
                    )
                except EmptyBufferException: continue

        else:
            raise Exception('Parse from buffer error: index are not correct ' + str(indices))

def serialize_to_buffer(
        message_iterator = None, # Message or tuples (with head on the first item.)
        signal = None,
        indices: Union[protobuf.pyext.cpp_message.GeneratedProtocolMessageType, dict] = None,
        partitions_model: Union[list, dict] = None,
        mem_manager = None
    ) -> Generator[buffer_pb2.Buffer, None, None]:  # method: indice
    try:
        if not message_iterator: message_iterator = buffer_pb2.Empty()
        if not indices: indices = {}
        if not partitions_model: partitions_model = [buffer_pb2.Buffer.Head.Partition()]
        if not signal: signal = Signal(exist=False)
        if not mem_manager: mem_manager = Enviroment.mem_manager
        if type(indices) is protobuf.pyext.cpp_message.GeneratedProtocolMessageType: indices = {1: indices}
        if type(indices) is not dict: raise Exception
    
        if type(partitions_model) is list: partitions_model = {1: partitions_model}  # Only've one index.
        if type(partitions_model) is not dict: raise Exception
        for i in indices.keys():
            if i in partitions_model:
                if type(partitions_model[i]) is buffer_pb2.Buffer.Head.Partition: partitions_model[i] = [partitions_model[i]]
                if type(partitions_model[i]) is not list: raise Exception
            else:
                partitions_model.update({i: [buffer_pb2.Buffer.Head.Partition()]})
    
        if not hasattr(message_iterator, '__iter__') or type(message_iterator) is tuple:
            message_iterator = itertools.chain([message_iterator])

        if 1 not in indices:
            message_type = next(message_iterator)
            indices.update({1: message_type[0]}) if type(message_type) is tuple else indices.update({1: type(message_type)})
            message_iterator = itertools.chain([message_type], message_iterator)
        
        indices = {e[1]: e[0] for e in indices.items()}
    except:
        raise Exception('Serialzie to buffer error: Indices are not correct ' + str(indices) + str(partitions_model))
    
    def send_file(filedir: Dir, signal: Signal) -> Generator[buffer_pb2.Buffer, None, None]:
        for b in get_file_chunks(
                filename=filedir.name, 
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
            mem_manager = Enviroment.mem_manager,
        ) -> Generator[buffer_pb2.Buffer, None, None]:

        message_bytes = message_to_bytes(message = message)
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

            signal.wait()
            file = generate_random_dir()
            with open(file, 'wb') as f, mem_manager(len=len(message_bytes)):
                f.write(message_bytes)
            try:
                for b in get_file_chunks(
                    filename=file,
                    signal=signal
                ): yield b
            finally:
                remove_file(file)

            try:
                yield buffer_pb2.Buffer(
                    separator = True
                )
            finally: signal.wait()

    for message in message_iterator:
        if type(message) is tuple:  # If is partitioned
            yield buffer_pb2.Buffer(
                head = buffer_pb2.Buffer.Head(
                    index = indices[message[0]],
                    partitions = partitions_model[indices[message[0]]]
                )
            )
            
            for partition in message[1:]:
                if type(partition) is Dir:
                    for b in send_file(
                        filedir = partition,
                        signal=signal
                    ): yield b
                else:
                    for b in send_message(
                        signal=signal,
                        message=partition,
                        mem_manager=mem_manager,
                    ): yield b

        else:  # If message is a protobuf object.
            head = buffer_pb2.Buffer.Head(
                index = indices[type(message)],
                partitions = partitions_model[indices[type(message)]]
            )
            for b in send_message(
                signal=signal,
                message=message,
                head=head,
                mem_manager=mem_manager,
            ): yield b

def client_grpc(
        method,
        input = None,
        timeout = None, 
        indices_parser: Union[protobuf.pyext.cpp_message.GeneratedProtocolMessageType, dict] = None,
        partitions_parser: Union[list, dict] = None,
        partitions_message_mode_parser: Union[bool, list, dict] = None,
        indices_serializer: Union[protobuf.pyext.cpp_message.GeneratedProtocolMessageType, dict] = None,
        partitions_serializer: Union[list, dict] = None,
        mem_manager = None,
        yield_remote_partition_dir_on_serializer: bool = False,
    ): # indice: method
    if not indices_parser: 
        indices_parser = buffer_pb2.Empty
        partitions_message_mode_parser = True
    if not partitions_message_mode_parser: partitions_message_mode_parser = False
    if not partitions_parser: partitions_parser = [buffer_pb2.Buffer.Head.Partition()]
    if not indices_serializer: indices_serializer = {}
    if not partitions_serializer: partitions_serializer = [buffer_pb2.Buffer.Head.Partition()]
    if not mem_manager: mem_manager = Enviroment.mem_manager
    signal = Signal()
    for b in parse_from_buffer(
        request_iterator = method(
                            serialize_to_buffer(
                                message_iterator = input if input else buffer_pb2.Empty(),
                                signal = signal,
                                indices = indices_serializer,
                                partitions_model = partitions_serializer,
                                mem_manager = mem_manager,
                            ),
                            timeout = timeout
                        ),
        signal = signal,
        indices = indices_parser,
        partitions_model = partitions_parser,
        partitions_message_mode = partitions_message_mode_parser,
        yield_remote_partition_dir = yield_remote_partition_dir_on_serializer,
    ): yield b


"""
    Get partitions and return the protobuff buffer.
"""
def partitions_to_buffer(
    message: protobuf.pyext.cpp_message.GeneratedProtocolMessageType,
    partitions_model: tuple,
    partitions: tuple
    ) -> str:
    total_len = 0
    for partition in partitions:
        if type(partition) is str:
            total_len += 2* os.path.getsize(partition)
        elif type(partition) is object:
            total_len += 2* sys.getsizeof(partition)
        elif type(partition) is bytes:
            total_len += len(partition)
        else:
            raise Exception('Partition to buffer error: partition type is wrong: ' + str(type(partition)))
    with Enviroment.mem_manager(len = total_len):
        return combine_partitions(
            obj_cls = message,
            partitions_model = partitions_model,
            partitions = partitions
        ).SerializeToString()

"""
    Serialize Object to plain bytes serialization.
"""
def serialize_to_plain(object: object) -> bytes:
    pass
