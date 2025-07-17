import os
from typing import Optional, Generator, Set, Tuple

from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2 as celaut
from protos import celaut_pb2
from protos.gateway_bee import StartService_input_indices, \
    StartService_input_message_mode
from src.gateway.utils import save_service
from src.utils import logger as log
from src.utils.config import SHA3_256_ID
from src.manager.maintain import add_wanted
from src.utils.config import ConfigManager

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")


class BreakIteration(Exception):
    pass


def find_service_hash(_hash: celaut.Metadata.HashTag.Hash) \
        -> Tuple[Optional[str], bool]:
    if SHA3_256_ID == _hash.type:
        value = _hash.value.hex()
        registry = os.listdir(REGISTRY)
        return value, value in registry
    else:
        return (None, False)


class Hash:
    def __init__(self, _hash: celaut.Metadata.HashTag.Hash):
        self.type = _hash.type
        self.value = _hash.value

    def __hash__(self):
        return hash((self.type, self.value))

    def __eq__(self, other):
        return (self.type, self.value) == (other.type, other.value)

    def proto(self) -> celaut.Metadata.HashTag.Hash:
        return celaut.Metadata.HashTag.Hash(
            type=self.type,
            value=self.value
        )


class AbstractInputServiceIterable:

    def __init__(self, request_iterator, context):
        self.parser_iterator = bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=StartService_input_indices,
            partitions_message_mode=StartService_input_message_mode
        )

        self.context = context

        self.configuration: Optional[celaut_pb2.Configuration] = None

        self.client_id = None
        self.recursion_guard_token = None

        self.service_hash: Optional[str] = None
        self.service_saved = False
        self.generated = False

        self.hashes: Set[Hash] = set()
        self.metadata: Optional[celaut.Metadata] = None

    def __pattern_matching(self, r) -> Generator[buffer_pb2.Buffer, None, None]:

        match type(r):
            case celaut_pb2.Client:
                self.client_id = r.client_id

            case celaut_pb2.RecursionGuard:
                self.recursion_guard_token = r.token

            case celaut_pb2.Configuration:
                self.configuration = r

            case celaut.Metadata.HashTag.Hash:
                self.hashes.add(Hash(r))
                if not self.service_hash:
                    self.service_hash, self.service_saved = find_service_hash(_hash=r)

            case celaut.Metadata:
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

                integrity_list = [h.type for h in self.metadata.hashtag.hash]
                if len(integrity_list) != len(set(integrity_list)):
                    log.LOGGER(f"There is an issue with the metadata received for the service {self.service_hash} (contains the individual hashes sent too).")
                    for hash in list(self.metadata.hashtag.hash):
                        log.LOGGER(f"-  {hash.type.hex()}: {hash.value.hex()}")
                    raise Exception("Metadata hash integrity error after receive it.")
                
                # Service specification format could be great to be checked.

            case bee.Dir:
                if r.type != celaut.Service:
                    raise Exception('Incorrect service message.')

                # Take it from metadata.
                if not self.service_hash:
                    # TODO  compute the hash of r.dir.
                    raise Exception("No service hash to allow service to be stored.")

                if self.metadata:
                    integrity_list = [h.type for h in self.metadata.hashtag.hash]
                    if len(integrity_list) != len(set(integrity_list)):
                        log.LOGGER(f"There is an issue with the metadata before save the service {self.service_hash} (contains the individual hashes sent too).")
                        for hash in list(self.metadata.hashtag.hash):
                            log.LOGGER(f"-  {hash.type.hex()}: {hash.value.hex()}")
                        raise Exception("Metadata hash integrity error before save it.")

                self.service_saved = save_service(
                    metadata=self.metadata,
                    service_dir=r.dir,
                    service_hash=self.service_hash
                )

        if self.service_saved and not self.generated:
            yield buffer_pb2.Buffer(signal=True)

            if not self.metadata:
                with open(METADATA_REGISTRY + self.service_hash, 'rb') as f:
                    self.metadata = celaut.Metadata()
                    self.metadata.ParseFromString(f.read())

            yield from self.generate()
            self.generated = True

    def __iter__(self):
        self.start()
        try:
            yield from (t for r in self.parser_iterator for t in self.__pattern_matching(r))
        finally:
            self.final()

    def start(self):
        pass

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        pass

    def final(self):
        if self.service_hash and not self.service_saved:
            add_wanted(self.service_hash)
