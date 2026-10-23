import concurrent.futures
import os
from typing import Optional, Generator, Set, Tuple

from protos import celaut_pb2 as celaut
from protos import celaut_pb2
from protos.gateway_bee import StartService_input_message_mode, rpc_input
from src.gateway.client_gate import ClientRequired, require_caller
from src.gateway.utils import save_service
from src.utils import logger as log
from src.utils.bee_client import BeeClient, Buffer, Dir, StreamControl
from src.utils.hashing import get_configured_hash_id
from src.manager.maintain import add_wanted
from src.utils.config import ConfigManager
from src.utils.tools.recursion_guard import received_hops

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
CONFIGURED_HASH_ID = get_configured_hash_id(env_manager)


class BreakIteration(Exception):
    pass


# How long the handler waits for the parser before it looks for skip requests to send.
# A skip request is queued while a block of the Service is being read, which can take a
# long time, so the handler cannot only look for them between two parsed messages. The
# wait is short because the sender goes on with the body of the block until the request
# arrives. A message that the parser has ready does not wait for it.
SKIP_POLL_SECONDS = 0.005

_END = object()


def find_service_hash(_hash: celaut.Metadata.HashTag.Hash) \
        -> Tuple[Optional[str], bool]:
    if CONFIGURED_HASH_ID == _hash.type:
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

    # The Gateway RPC this iterable serves: its request indices come from
    # protos.gateway_bee.GATEWAY_RPCS, the table this node also announces.
    RPC: str = "StartService"

    # Class-level too, so an iterable built without __init__ (a handler under test)
    # reads "the sender gave no hop count" rather than failing on the attribute.
    recursion_guard_hops: Optional[int] = None

    # Also at class level: with no control, the request is read with no skip requests.
    control: Optional[StreamControl] = None

    def __init__(self, request_iterator, context):
        # The parse side queues a skip request when a block that this node holds starts
        # to arrive (bee_rpc.control.StreamControl, issue #371), and __iter__ sends it
        # in the response direction. A sender that asked for it (`block_skip`) stops
        # the body of the block. Any other sender gets a Buffer that its parser drops.
        self.control = StreamControl()
        self.parser_iterator = BeeClient.parse(
            request_iterator,
            indices=rpc_input(self.RPC),
            partitions_message_mode=StartService_input_message_mode,
            control=self.control,
        )

        self.context = context

        self.configuration: Optional[celaut_pb2.Configuration] = None

        self.client_id = None
        # The RecursionGuard as received (#456): the token, and the hops left when the
        # sender said (None when it did not -- a root, or a node predating the field).
        self.recursion_guard_token = None
        self.recursion_guard_hops = None
        self._caller_checked = False

        self.service_hash: Optional[str] = None
        self.service_saved = False
        self.generated = False

        self.hashes: Set[Hash] = set()
        self.metadata: Optional[celaut.Metadata] = None

    def __pattern_matching(self, r) -> Generator[Buffer, None, None]:

        match type(r):
            case celaut_pb2.Client:
                self.client_id = r.client_id

            case celaut_pb2.RecursionGuard:
                self.recursion_guard_token = r.token
                self.recursion_guard_hops = received_hops(r)

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

            case Dir:
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
            # DDOS protection (issue #428): checked here rather than in start(),
            # because a Client can arrive anywhere in this stream (order is the
            # caller's choice, like every field of this envelope) and is not
            # necessarily parsed yet the moment the service looks ready.
            #
            # A client_id already seen (right or wrong) is a real, final answer --
            # refuse now. But *no* client_id yet is not the same as "will never send
            # one": it may simply not have arrived on the wire yet, so this defers
            # instead of refusing, and retries on every later message until either a
            # client_id shows up or the stream ends (final(), below, makes the
            # refusal definitive in the latter case). Nothing has been yielded yet at
            # this point either way, so deferring never leaves a half-sent response.
            if not self._caller_checked:
                # The check runs once. After it gives an answer (a pass or a
                # refusal), final() does not run it again: each call counts against
                # the client's rate limit, and a second call could replace the real
                # error of generate() with "calling too fast".
                decided = True
                try:
                    require_caller(self.context, self.client_id or "")
                except ClientRequired:
                    if not self.client_id:
                        decided = False
                        return
                    raise
                finally:
                    self._caller_checked = decided

            yield Buffer(signal=True)

            if not self.metadata:
                with open(METADATA_REGISTRY + self.service_hash, 'rb') as f:
                    self.metadata = celaut.Metadata()
                    self.metadata.ParseFromString(f.read())

            yield from self.generate()
            self.generated = True

    def _parsed(self) -> Generator[object, None, None]:
        """The messages of the request, and the skip requests for the sender.

        A message is parsed on a worker thread, one at a time, so the order of the
        handler does not change: the next message is read when the handler asks for it.
        While the worker reads a message (a Service of many blocks is one message), this
        thread sends the skip requests that the reading queues. They are Buffers; no
        message of the request is one.
        """
        if self.control is None:
            yield from self.parser_iterator
            return
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            while True:
                future = executor.submit(next, self.parser_iterator, _END)
                while True:
                    try:
                        message = future.result(timeout=SKIP_POLL_SECONDS)
                        break
                    except concurrent.futures.TimeoutError:
                        yield from self.control.pending_outbound()
                # A skip that the last message queued goes before the next answer.
                yield from self.control.pending_outbound()
                if message is _END:
                    return
                yield message
        finally:
            # A worker that waits for the request is not killed. It ends with the call.
            executor.shutdown(wait=False)

    def __iter__(self):
        self.start()
        try:
            for r in self._parsed():
                if type(r) is Buffer:
                    yield r
                else:
                    yield from self.__pattern_matching(r)
            # Only when the request stream ends, not when the caller cancels it. Neither
            # side has the service: the node does not hold it, and the request did not
            # carry it. Answering nothing left the caller to guess why.
            if not self.service_saved:
                raise Exception(self._missing_service_reason())
        finally:
            self.final()

    def _missing_service_reason(self) -> str:
        if self.service_hash:
            return (
                f"This node does not have the service {self.service_hash}, and the "
                "request does not contain it."
            )
        return (
            "The request gives no hash of the type this node uses for its registry "
            f"({CONFIGURED_HASH_ID.hex()}), and no service."
        )

    def start(self):
        pass

    def generate(self) -> Generator[Buffer, None, None]:
        pass

    def final(self):
        if self.service_hash and not self.service_saved:
            add_wanted(self.service_hash)
        elif self.service_saved and not self._caller_checked:
            # The stream ended with the service ready to serve but no client_id ever
            # arrived to confirm a caller -- what looked like "might still be coming"
            # above never came. Nothing was sent for this request (the check above
            # runs before the first byte of the response), so it is safe to refuse
            # now instead of silently answering nothing.
            require_caller(self.context, self.client_id or "")
