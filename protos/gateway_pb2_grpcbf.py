import capnp
from protos import gateway_pb2, service_capnp

StartService_input = {
    5 : gateway_pb2.Client,
    6 : gateway_pb2.RecursionGuard,

    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : service_capnp.ServiceWithMeta,
    3 : gateway_pb2.HashWithConfig,
    4 : gateway_pb2.ServiceWithConfig
}

GetServiceEstimatedCost_input = {
    5 : gateway_pb2.Client,
    6 : gateway_pb2.RecursionGuard,

    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : service_capnp.ServiceWithMeta,
    3 : gateway_pb2.HashWithConfig,
    4 : gateway_pb2.ServiceWithConfig,
}

GetServiceTar_input = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : service_capnp.ServiceWithMeta,
}