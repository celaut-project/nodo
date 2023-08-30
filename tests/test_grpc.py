from grpcbigbuffer.client import serialize_to_buffer, Dir, parse_from_buffer

from tests.main import SORTER
from protos import gateway_pb2

buffer = serialize_to_buffer(
    message_iterator=(gateway_pb2.ServiceWithMeta, Dir('__registry__/' + SORTER)),
    indices=gateway_pb2.ServiceWithMeta,
)

object = parse_from_buffer(
    request_iterator=buffer,
    indices=gateway_pb2.ServiceWithMeta,
    partitions_message_mode=False
)

for o in object:
    print(o)
