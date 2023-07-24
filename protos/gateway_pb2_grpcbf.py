from protos import gateway_pb2, compile_pb2

StartService_input = {
    5: gateway_pb2.Client,
    6: gateway_pb2.RecursionGuard,

    1: gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2: gateway_pb2.ServiceWithMeta,
    3: gateway_pb2.HashWithConfig,
    4: gateway_pb2.ServiceWithConfig
}

"""
    // ( celaut.Any.Metadata.HashTag.Hash=H, celaut.Any=S, celaut.Configuration=C; { H v S v H^C v S^C } )

    // 2. S partition [(1, 2.4, 3, 4), (2.1, 2.2, 2.3)]

    // 3. H^C 
    message HashWithConfig { 
        celaut.Any.Metadata.HashTag.Hash hash = 1;
        celaut.Configuration config = 3;  
    }

    // 4. S^C  partition [(1, 2.4, 3, 4), (2.1, 2.2, 2.3)]
    message ServiceWithConfig { 
        celaut.Any service = 2;
        celaut.Configuration config = 3;
    }
.proto
"""

GetServiceEstimatedCost_input = {
    5: gateway_pb2.Client,
    6: gateway_pb2.RecursionGuard,

    1: gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2: gateway_pb2.ServiceWithMeta,
    3: gateway_pb2.HashWithConfig,
    4: gateway_pb2.ServiceWithConfig,
}


CompileOutput_indices = {
    1: compile_pb2.CompileOutputServiceId,
    2: compile_pb2.ServiceWithMeta
}
