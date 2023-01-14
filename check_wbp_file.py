from grpcbigbuffer import client as grpcbigbuffer
from hashlib import sha3_256
from protos.gateway_pb2 import ServiceWithMeta

h = sha3_256()

service = ServiceWithMeta()

service.ParseFromString(
    open('__cache__/grpcbigbuffer/12204466/wbp.bin').read()
)

for i in grpcbigbuffer.send_message(service.service):
    h.update(i.chunk)

print(h.hexdigest())
