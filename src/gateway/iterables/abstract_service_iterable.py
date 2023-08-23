import os
from typing import Optional, Generator, Set, Tuple

from grpcbigbuffer import client as grpcbf, buffer_pb2

from protos import celaut_pb2 as celaut
from protos import gateway_pb2
from protos.gateway_pb2_grpcbf import StartService_input_indices, \
    StartService_input_message_mode
from src.gateway.utils import save_service
from src.manager.manager import could_ve_this_sysreq
from src.utils import logger as l
from src.utils.env import SHA3_256_ID, \
    REGISTRY, METADATA_REGISTRY


class BreakIteration(Exception):
    pass


def find_service_hash(_hash: gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash) \
        -> Tuple[Optional[str], bool]:
    return _hash.value.hex(), _hash.value.hex() in [s for s in os.listdir(REGISTRY)] if SHA3_256_ID == _hash.type \
        else (None, False)


def combine_metadata(service_hash: str, request_metadata: Optional[celaut.Any.Metadata]) -> celaut.Any.Metadata:
    disk_metadata = celaut.Any.Metadata()
    with open(METADATA_REGISTRY + service_hash, 'rb') as f:
        disk_metadata.ParseFromString(f.read())

    if request_metadata:
        combined_metadata = celaut.Any.Metadata()
        combined_metadata.MergeFrom(disk_metadata)
        combined_metadata.MergeFrom(request_metadata)
        return combined_metadata
    else:
        return disk_metadata


class Hash:
    def __init__(self, _hash: gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash):
        self.type = _hash.type
        self.value = _hash.value

    def __hash__(self):
        return hash((self.type, self.value))

    def __eq__(self, other):
        return (self.type, self.value) == (other.type, other.value)

    def proto(self) -> gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash:
        return gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash(
            type=self.type,
            value=self.value
        )


class AbstractServiceIterable:
    configuration: Optional[gateway_pb2.Configuration] = None

    client_id = None
    recursion_guard_token = None

    service_hash: Optional[str] = None
    service_saved = False

    hashes: Set[Hash] = set()
    metadata: Optional[gateway_pb2.celaut__pb2.Any.Metadata] = None

    def __init__(self, request_iterator, context):
        self.parser_iterator = grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices=StartService_input_indices,
            partitions_message_mode=StartService_input_message_mode
        )
        self.context = context

    def __pattern_matching(self, r) -> Generator[buffer_pb2.Buffer, None, None]:
        match type(r):
            case gateway_pb2.Client:
                self.client_id = r.client_id

            case gateway_pb2.RecursionGuard:
                self.recursion_guard_token = r.token

            case gateway_pb2.Configuration:
                if r.HasField('max_sysreq') and not could_ve_this_sysreq(sysreq=r.max_sysreq):
                    raise Exception("The node can't execute the service with this requeriments.")

                self.configuration = r

            case celaut.Any.Metadata.HashTag.Hash:
                self.hashes.add(Hash(r))
                if not self.service_hash:
                    self.service_hash, self.service_saved = find_service_hash(_hash=r)

            case celaut.Any.Metadata:
                self.metadata = r
                for _hash in self.metadata.hashtag.hash:  # TODO nos podríamos ahorrar esta iteración
                    if not self.service_hash:
                        self.service_hash, self.service_saved = find_service_hash(_hash=_hash)
                    # TODO se podría realizar junto con la iteració siguiente:

                # Combine the hash list with the metadata hashes.
                self.hashes: Set[Hash] = self.hashes.union({
                    Hash(_e) for _e in self.metadata.hashtag.hash
                })
                self.metadata.hashtag.ClearField("hash")
                self.metadata.hashtag.hash.extend([_e.proto() for _e in self.hashes])
                self.hashes.clear()

            case grpcbf.Dir:
                if r.type != gateway_pb2.celaut__pb2.Service:
                    raise Exception('Incorrect service message.')

                #  -- TODO --
                #  NO TIENE SENTIDO USAR AQUI EL DUPLICATE GRABBER YA QUE AHORA NO RETORNAMOS PRIMERO EL TIPO, SI NO QUE
                #  RETORNAMOS EL TIPO JUNTO CON EL DIRECTORIO DEL SERVICIO YA DESCARGADO. EL DUPLICATE GRABBER DEBERIA
                #  USARSE ANTES.

                l.LOGGER('Save service on disk')
                # Take it from metadata.
                if not self.service_hash:
                    # TODO  compute the hash of r.dir.
                    raise Exception("Not registry hash.")

                self.service_saved = save_service(
                    metadata=self.metadata,
                    service_dir=r.dir,
                    service_hash=self.service_hash
                )

        if self.service_saved:
            yield buffer_pb2.Buffer(signal=True)
            self.metadata = combine_metadata(
                service_hash=self.service_hash, request_metadata=self.metadata
            )

            try:
                yield from self.generate()
            except BreakIteration:
                raise StopIteration

    def __iter__(self):
        self.start()
        yield from (t for r in self.parser_iterator for t in self.__pattern_matching(r))
        self.final()

    def start(self):
        pass

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        pass

    def final(self):
        pass
