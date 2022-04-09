# Generated by the gRPC Python protocol compiler plugin. DO NOT EDIT!
"""Client and server classes corresponding to protobuf-defined services."""
import grpc

import buffer_pb2 as buffer__pb2


class GatewayStub(object):
    """GRPC.

    """

    def __init__(self, channel):
        """Constructor.

        Args:
            channel: A grpc.Channel.
        """
        self.StartService = channel.stream_stream(
                '/gateway.Gateway/StartService',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.StopService = channel.stream_stream(
                '/gateway.Gateway/StopService',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.Hynode = channel.stream_stream(
                '/gateway.Gateway/Hynode',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.ModifyServiceSystemResources = channel.stream_stream(
                '/gateway.Gateway/ModifyServiceSystemResources',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.GetSystemResources = channel.stream_stream(
                '/gateway.Gateway/GetSystemResources',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.GetFile = channel.stream_stream(
                '/gateway.Gateway/GetFile',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.Compile = channel.stream_stream(
                '/gateway.Gateway/Compile',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.GetServiceTar = channel.stream_stream(
                '/gateway.Gateway/GetServiceTar',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )
        self.GetServiceCost = channel.stream_stream(
                '/gateway.Gateway/GetServiceCost',
                request_serializer=buffer__pb2.Buffer.SerializeToString,
                response_deserializer=buffer__pb2.Buffer.FromString,
                )


class GatewayServicer(object):
    """GRPC.

    """

    def StartService(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def StopService(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def Hynode(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def ModifyServiceSystemResources(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def GetSystemResources(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def GetFile(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def Compile(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def GetServiceTar(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def GetServiceCost(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')


def add_GatewayServicer_to_server(servicer, server):
    rpc_method_handlers = {
            'StartService': grpc.stream_stream_rpc_method_handler(
                    servicer.StartService,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'StopService': grpc.stream_stream_rpc_method_handler(
                    servicer.StopService,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'Hynode': grpc.stream_stream_rpc_method_handler(
                    servicer.Hynode,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'ModifyServiceSystemResources': grpc.stream_stream_rpc_method_handler(
                    servicer.ModifyServiceSystemResources,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'GetSystemResources': grpc.stream_stream_rpc_method_handler(
                    servicer.GetSystemResources,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'GetFile': grpc.stream_stream_rpc_method_handler(
                    servicer.GetFile,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'Compile': grpc.stream_stream_rpc_method_handler(
                    servicer.Compile,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'GetServiceTar': grpc.stream_stream_rpc_method_handler(
                    servicer.GetServiceTar,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
            'GetServiceCost': grpc.stream_stream_rpc_method_handler(
                    servicer.GetServiceCost,
                    request_deserializer=buffer__pb2.Buffer.FromString,
                    response_serializer=buffer__pb2.Buffer.SerializeToString,
            ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
            'gateway.Gateway', rpc_method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


 # This class is part of an EXPERIMENTAL API.
class Gateway(object):
    """GRPC.

    """

    @staticmethod
    def StartService(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/StartService',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def StopService(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/StopService',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def Hynode(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/Hynode',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def ModifyServiceSystemResources(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/ModifyServiceSystemResources',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def GetSystemResources(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/GetSystemResources',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def GetFile(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/GetFile',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def Compile(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/Compile',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def GetServiceTar(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/GetServiceTar',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def GetServiceCost(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/gateway.Gateway/GetServiceCost',
            buffer__pb2.Buffer.SerializeToString,
            buffer__pb2.Buffer.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)
