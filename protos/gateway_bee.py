from protos import celaut_pb2, pack_pb2

StartService_input_indices = {
    1: celaut_pb2.Client,
    2: celaut_pb2.RecursionGuard,
    3: celaut_pb2.Configuration,
    4: celaut_pb2.Metadata.HashTag.Hash,
    5: celaut_pb2.Metadata,
    6: celaut_pb2.Service,
}
StartService_input_message_mode = {1: True, 2: True, 3: True, 4: True, 5: True, 6: False}  # False yield a Dir.

# GetResourceAvailability (#459) took `1: ArchitectureResources, 2: Client`; the
# RecursionGuard (#456) is appended as 3 rather than renumbering, so a caller that
# predates it is parsed exactly as before. A server that predates it refuses index 3
# outright (bee-rpc rejects an index it was not given), which is why the client only
# sends it when it has a token to forward -- see
# `workload_admission.check_resource_availability_on_peer`.
GetResourceAvailability_input_indices = {
    1: celaut_pb2.ArchitectureResources,
    2: celaut_pb2.Client,
    3: celaut_pb2.RecursionGuard,
}

# GenerateClient answers with one of two messages (issue #361), so both ends need the
# same index for each: bee-rpc numbers a lone message 1 by itself, which would put a
# Client and a PoWRequired on the same index and leave the caller unable to tell which
# it received.
GenerateClient_output_indices = {
    1: celaut_pb2.Client,
    2: celaut_pb2.PoWRequired,
}

PackOutput_indices = {
    1: pack_pb2.PackOutputServiceId,
    2: celaut_pb2.Metadata,
    3: pack_pb2.Service,
    4: pack_pb2.PackOutputError
}
