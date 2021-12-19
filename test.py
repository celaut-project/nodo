import grpc, gateway_pb2_grpc
from concurrent import futures
from gateway_pb2_grpcbf import StartService_input, StartService_input_partitions_v2
import grpcbigbuffer
import gateway_pb2, celaut_pb2

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
                for b in grpcbigbuffer.serialize_to_buffer(
                    message_iterator = (gateway_pb2.ServiceWithMeta,
                        '__registry__/01d030604fc89032faf57b399098db819f4ec776c0419e86cdaf64d2217014f7/p1',
                        '__registry__/01d030604fc89032faf57b399098db819f4ec776c0419e86cdaf64d2217014f7/p2'),
                    partitions_model=StartService_input_partitions_v2,
                    indices=StartService_input
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


