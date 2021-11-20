import grpc, gateway_pb2_grpc
from concurrent import futures
import grpcbigbuffer
import gateway_pb2, buffer_pb2


class Gateway(gateway_pb2_grpc.Gateway):
    def StartService(self, request_iterator, context):
        parser = grpcbigbuffer.parse_from_buffer(
            request_iterator=request_iterator, 
            indices=gateway_pb2.TokenMessage,
            partitions_message_mode = True
        )
        while True:
            try:
                r = next(parser)
            except StopIteration: break
            print(r)
            if type(r) is gateway_pb2.TokenMessage:
                meta = gateway_pb2.celaut__pb2.Any.Metadata(
                    complete  = True
                )
                for b in grpcbigbuffer.serialize_to_buffer(
                    partitions_model = [
                        buffer_pb2.Buffer.Head.Partition(index={1: buffer_pb2.Buffer.Head.Partition(), 2: buffer_pb2.Buffer.Head.Partition()}),
                        buffer_pb2.Buffer.Head.Partition(index={3: buffer_pb2.Buffer.Head.Partition()}),
                    ],
                    message_iterator=(gateway_pb2.Instance, 'asd', # TODO the partition obj is a str, but lib thinks that i give a file dir.
                    gateway_pb2.Instance(
                        instance_meta=meta
                        ))
                ): yield b


# create a gRPC server
server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
gateway_pb2_grpc.add_GatewayServicer_to_server(
    Gateway(), server=server
)

server.add_insecure_port('[::]:' + str(8080))
print('Starting gateway at port'+ str(8080))
server.start()
server.wait_for_termination()
