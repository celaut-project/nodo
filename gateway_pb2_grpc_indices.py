import gateway_pb2

StartService_indices = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : gateway_pb2.celaut__pb2.Service,
    3: gateway_pb2.HashWithConfig,
    4: gateway_pb2.ServiceWithConfig
}

GetServiceCost_indices = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : gateway_pb2.celaut__pb2.Service,
}

GetServiceTar_indices = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : gateway_pb2.celaut__pb2.Service,
}