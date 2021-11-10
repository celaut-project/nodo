import buffer_pb2, gateway_pb2

# This is part of the transport protocol (slot) data.
StartService_input = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : gateway_pb2.celaut__pb2.Any,
    3 : gateway_pb2.HashWithConfig,
    4 : gateway_pb2.ServiceWithConfig
}

StartService_input_partitions = {
    2 : [
            buffer_pb2.Buffer.Head.Partition(index={
                1 : buffer_pb2.Buffer.Head.Partition(),
                2 : buffer_pb2.Buffer.Head.Partition(index={
                    1 : buffer_pb2.Buffer.Head.Partition(index={
                        3 : buffer_pb2.Buffer.Head.Partition(),
                        4 : buffer_pb2.Buffer.Head.Partition(),
                    }),
                    2 : buffer_pb2.Buffer.Head.Partition(),
                    3 : buffer_pb2.Buffer.Head.Partition(),
                    4 : buffer_pb2.Buffer.Head.Partition(),
                })
            }),
            buffer_pb2.Buffer.Head.Partition(index={
                2 : buffer_pb2.Buffer.Head.Partition(index={
                    1 : buffer_pb2.Buffer.Head.Partition(index={
                        1 : buffer_pb2.Buffer.Head.Partition(),
                        2 : buffer_pb2.Buffer.Head.Partition()
                    }),
                })
            })
        ],
    4 : [
            buffer_pb2.Buffer.Head.Partition(index={
                2 : buffer_pb2.Buffer.Head.Partition(index={
                    1 : buffer_pb2.Buffer.Head.Partition(),
                    2 : buffer_pb2.Buffer.Head.Partition(index={
                        1 : buffer_pb2.Buffer.Head.Partition(index={
                            3 : buffer_pb2.Buffer.Head.Partition(),
                            4 : buffer_pb2.Buffer.Head.Partition(),
                        }),
                        2 : buffer_pb2.Buffer.Head.Partition(),
                        3 : buffer_pb2.Buffer.Head.Partition(),
                        4 : buffer_pb2.Buffer.Head.Partition(),
                    })
                }),
                3 : buffer_pb2.Buffer.Head.Partition()
            }),
            buffer_pb2.Buffer.Head.Partition(index={
                2 : buffer_pb2.Buffer.Head.Partition(index={
                    2 : buffer_pb2.Buffer.Head.Partition(index={
                        1 : buffer_pb2.Buffer.Head.Partition(index={
                            1 : buffer_pb2.Buffer.Head.Partition(),
                            2 : buffer_pb2.Buffer.Head.Partition()
                        }),
                    })
                })
            })
        ]
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


GetServiceCost_input = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : gateway_pb2.celaut__pb2.Any,
}

GetServiceTar_input = {
    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : gateway_pb2.celaut__pb2.Any,
}