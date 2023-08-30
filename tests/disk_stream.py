import hashlib
from io import BytesIO

from typing import Generator, List, Dict, Tuple
import os

from grpcbigbuffer import buffer_pb2
import numpy as np
from grpcbigbuffer.utils import encode_bytes


################
## Validation ##
################
def to_dict(partitions):
    l = {}

    def recursive(model, dir, prev_i=''):
        l = {}
        for index, partition in model.index.items():
            i_name: str = prev_i + '.' + str(index) if prev_i != '' else str(index)
            if len(partition.index) > 0:
                l.update(recursive(partition, dir, i_name))
            else:
                l[i_name] = dir
        return l

    for dir, model in partitions.items():
        l.update(recursive(model, dir))

    return l


def sort(d):
    return sorted(d.items())


def get_parsers(l):
    r = []
    f = []
    n = []
    aux = None
    for e in l:
        if e[1] != aux:
            aux = e[1]
            if e[1] in n:
                f.append(e[1])
            else:
                n.append(e[1])

        if len(r) == 0 or r[-1][0] != e[1]:
            r.append((e[1], [e[0]]))
        else:
            r[-1][1].append(e[0])
    return r, f


def check_sorted_list(l) -> bool:
    n = []
    aux = None
    for e in l:
        if e[1] != aux:
            aux = e[1]
            if e[1] in n:
                return False
            else:
                n.append(e[1])
    return True


def reorg_partitions(
        dirs: List[str],
        partitions: List[buffer_pb2.Buffer.Head.Partition]
):
    partitions = {dirs[i]: p for i, p in enumerate(partitions)}
    dict = to_dict(partitions=partitions)
    sorted_list = sort(dict)
    sorted_and_grouped, list_for_parse = get_parsers(sorted_list)
    # ...


def validate_partitions(
        partitions: List[buffer_pb2.Buffer.Head.Partition]
) -> bool:
    return check_sorted_list(
        sort(
            to_dict(
                partitions={i: p for i, p in enumerate(partitions)}
            )
        )
    )


def calculate_hash_of_complete(
        dirs: List[str],
        partitions: List[buffer_pb2.Buffer.Head.Partition] = None,
        hash_function=None
) -> str:
    hash_id = hashlib.sha3_256() if not hash_function else hash_function()
    if partitions:
        for chunk in partition_disk_stream(
                dirs=dirs,
                partitions=partitions
        ):
            hash_id.update(chunk)

    else:
        f = open(dirs[0], 'rb')
        while True:
            data = f.read()
            if not data:
                break
            hash_id.update(data)
        f.close()

    return hash_id.hexdigest()


#################
### Streaming ###
#################

def shifting(bitlist: str) -> int:
    out = 0
    for bit in bitlist:
        out = (out << 1) | int(bit)
    return out


# returns varint encoded tag based upon field number and wire type
def get_tag(field_num: int) -> bytes:
    return (
        shifting(''.join([
            str(i) for i in
            np.unpackbits(
                np.array(
                    [[field_num]], dtype=np.uint8
                ), axis=1
            ).tolist()[0] + [0, 1, 0]
        ]))
    ).to_bytes(1, byteorder='little')


def get_field(tag: bytes) -> int:
    return decode_bytes(
        shifting(
            ''.join([
                str(i) for i in
                np.unpackbits(
                    np.array(
                        [[
                            decode_bytes(tag)
                        ]], dtype=np.uint8
                    ), axis=1
                ).tolist()[0][:-3]
            ])
        ).to_bytes(1, byteorder='little')
    )


def decode_bytes(buf):
    """Read a varint from from `buf` bytes"""

    def decode_stream(stream):
        """Read a varint from `stream`"""

        def _read_one(stream):
            """Read a byte from the file (as an integer)
            raises EOFError if the stream ends while reading bytes.
            """
            c = stream.read(1)
            if c == b'':
                raise EOFError("Unexpected EOF while reading bytes")
            return ord(c)

        shift = 0
        result = 0
        while True:
            i = _read_one(stream)
            result |= (i & 0x7f) << shift
            shift += 7
            if not (i & 0x80):
                break

        return result

    return decode_stream(BytesIO(buf))


class Partition:
    def __init__(self, file_path):
        self.file_path = file_path
        self.file = open(file_path, 'r+b')
        self.size = os.path.getsize(file_path)

    def next_index(self) -> Tuple[int, int]:
        index: bytes | None = None
        arr: bytes = b''
        while b := self.file.read(1):

            if not index:
                index: bytes = b
            else:
                arr += b
                if np.unpackbits(
                        np.array(
                            [[
                                np.frombuffer(b, dtype=np.uint8)
                            ]], dtype=np.uint8
                        ), axis=1
                ).tolist()[0][0][0] == 0: break

        return get_field(index), decode_bytes(arr)

    def read(self):
        while chunk := self.file.read(1024 * 10):
            yield chunk


class Index:
    def __init__(self, index: int):

        self.index: int = index
        self.on_multiple_partitions: bool = False
        self.alone_on_all_partitions: bool = True
        self.schema: List[Index] | None = None
        self.schema_d: Dict[int, Index] = {}
        self.file_partitions: List[Partition] = []
        self.is_alone_on_partition: List[bool] = []
        self.size = None
        self.pruned_bytes = 0
        self.name: str = str(self.index)

    def add_partition(self, partition: Partition, alone_on_it: bool):
        if self.signed(): raise Exception('Error, signed.')

        if len(self.file_partitions) == 1:
            self.on_multiple_partitions = True
        if alone_on_it is False:
            self.alone_on_all_partitions = False

        self.is_alone_on_partition.append(alone_on_it)
        self.file_partitions.append(partition)

    def add_indexes(self,
                    buf_partition: buffer_pb2.Buffer.Head.Partition,
                    dir_partition: Partition
                    ):
        if self.signed(): raise Exception('Error, signed.')
        if len(buf_partition.index) > 0:
            only_one: bool = len(buf_partition.index) == 1
            for index, sub_partition in buf_partition.index.items():
                if index not in self.schema_d.keys():
                    self.schema_d[index] = Index(index=index)

                index_obj: Index = self.schema_d[index]

                index_obj.add_partition(
                    partition=dir_partition,
                    alone_on_it=only_one
                )

                index_obj.add_indexes(
                    buf_partition=sub_partition,
                    dir_partition=dir_partition
                )

    def signed(self) -> bool:
        return self.schema is not None

    def sign(self):
        if self.signed(): raise Exception('Error, signed.')
        self.schema = list(self.schema_d.values())
        self.schema_d = None
        for index in self.schema:
            try:
                index.sign()
            except Exception as e:
                raise Exception(str(index.index) + ' ' + str(e))

        self.name = str(self.index) + '-' + str(len(self.schema)) + '-' + str(len(self.file_partitions)) + \
                    '-' + str(self.on_multiple_partitions) + '-' + str(self.alone_on_all_partitions)

    def get_size(self) -> int:
        if not self.size:
            self.compute_size()
        return self.size

    def get_pruned_bytes(self) -> int:
        if not self.size:
            self.compute_size()
        return self.pruned_bytes

    def compute_size(self):
        if not self.on_multiple_partitions and self.alone_on_all_partitions:
            self.size = self.file_partitions[0].size
            return

        elif self.on_multiple_partitions and False not in self.is_alone_on_partition:
            total_size = sum([p.size for p in self.file_partitions])

        elif self.on_multiple_partitions:  # and not self.alone_on_all_partitions
            total_size = 0
            pruned_bytes = 0
            for i, partition in enumerate(self.file_partitions):
                alone_on_it: bool = self.is_alone_on_partition[i]
                if not alone_on_it:
                    index, size = partition.next_index()
                    if index != self.index:
                        raise Exception('Partition disk stream error. Unexpected index ' \
                                        + str(index) + ' instead of ' + str(self.index))
                    pruned_bytes += len(encode_bytes(size)) + 1
                    if size == 0: continue

                    total_size += size

                else:
                    total_size += partition.size
            self.pruned_bytes = pruned_bytes

        else:  # not self.on_multiple_partitions and not self.alone_on_all_partitions
            raise Exception('Partition disk stream error.')  # No debería llegar hasta aqui.

        if total_size > 0:
            for i in self.schema:
                if i.on_multiple_partitions:
                    total_size += len(encode_bytes(i.get_size())) + 1
                    total_size -= i.get_pruned_bytes()

        self.size = total_size

    def generate(self) -> Generator[bytes, None, None]:
        if self.alone_on_all_partitions and not self.on_multiple_partitions:
            size: int = os.path.getsize(self.file_partitions[0].file_path)
            yield get_tag(self.index)
            yield encode_bytes(size)
            for i in self.file_partitions[0].read():
                yield i

        elif self.on_multiple_partitions:  # and (alone_on_all_partitions or not_alone_on_all_partitions)
            size: int = self.get_size()
            if size > 0:
                yield get_tag(self.index)
                yield encode_bytes(size)
                for i in self.schema:
                    for c in i.generate():
                        yield c

        else:  # not alone_on_all_partitions and not on_multiple_partitions
            # Partitions should be readen only on the first index on them. The rest index are not
            #  going to generate anything.
            for i in self.file_partitions[0].read():
                yield i


def reorg_by_indexes(
        dirs: List[str],
        partitions: List[buffer_pb2.Buffer.Head.Partition]
) -> List[Index]:
    # Not needed. if len(dirs) != len(partitions):  raise Exception('Partition disk stream error, incompatible inputs.')

    partition_obj_arr: List[Partition] = [Partition(file_path=d) for d in dirs]

    index_obj_d: Dict[int, Index] = {}
    for i, partition in enumerate(partitions):
        only_one: bool = len(partition.index) == 1
        for index, sub_partition in partition.index.items():
            if index not in index_obj_d.keys():
                index_obj_d[index] = Index(index=index)

            index_obj: Index = index_obj_d[index]

            index_obj.add_partition(
                partition=partition_obj_arr[i],
                alone_on_it=only_one
            )
            index_obj.add_indexes(
                buf_partition=sub_partition,
                dir_partition=partition_obj_arr[i]
            )

    index_obj_arr: List[Index] = list(index_obj_d.values())
    for index in index_obj_arr: index.sign()
    return index_obj_arr


def partition_disk_stream(
        dirs: List[str],
        partitions: List[buffer_pb2.Buffer.Head.Partition]
) -> Generator[bytes, None, None]:
    if len(dirs) != len(partitions):
        raise Exception('Partition disk stream error, incompatible inputs.')

    if len(partitions) < 2:
        raise Exception('Partition disk stream error, multiple partitions needed.')

    if not validate_partitions(partitions):
        # TODO reorg_partitions to a correct form.
        raise Exception('Partition model not correct.')

    #
    # Agrupa particiones para no repetir cabeceras.
    #
    # Calcula el tamaño de las cabeceras en función
    #   de lo que ocupa en todas las particiones
    #   donde se encuentra.
    #
    # Cuando se lee una partición cuyo mensaje
    #   ya se ha comenzado no debe añadir su cabecera
    #   y, en caso de no ser el principal, eliminarla a
    #   partir de su identificador y longitud del mensaje.
    #

    for index in reorg_by_indexes(
            dirs=dirs,
            partitions=partitions
    ):
        for b in index.generate():
            yield b
