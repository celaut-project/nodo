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

PackOutput_indices = {
    1: pack_pb2.PackOutputServiceId,
    2: celaut_pb2.Metadata,
    3: pack_pb2.Service,
    4: pack_pb2.PackOutputError
}
