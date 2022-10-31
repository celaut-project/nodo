from protos import buffer_pb2, gateway_pb2, service_capnp

StartService_input = {
    5 : gateway_pb2.Client,
    6 : gateway_pb2.RecursionGuard,

    1 : gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash,
    2 : service_capnp.ServiceWithMeta,
    3 : gateway_pb2.HashWithConfig,
    4 : gateway_pb2.ServiceWithConfig
}

StartService_input_partitions_v1 = {
    4: [
            buffer_pb2.Buffer.Head.Partition(index={
                3 : buffer_pb2.Buffer.Head.Partition(),
                4 : buffer_pb2.Buffer.Head.Partition(),
                5 : buffer_pb2.Buffer.Head.Partition(),
                6 : buffer_pb2.Buffer.Head.Partition(),
            }),
           buffer_pb2.Buffer.Head.Partition(index={
                2 : buffer_pb2.Buffer.Head.Partition()
            })
    ]
}

StartService_input_partitions_v2 = {
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
                3 : buffer_pb2.Buffer.Head.Partition(),
                4 : buffer_pb2.Buffer.Head.Partition(),
                5 : buffer_pb2.Buffer.Head.Partition(),
                6 : buffer_pb2.Buffer.Head.Partition(),
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

Compile_output_partitions_v1 = [
    buffer_pb2.Buffer.Head.Partition(index={1 : buffer_pb2.Buffer.Head.Partition()}),
    buffer_pb2.Buffer.Head.Partition(index={2 : buffer_pb2.Buffer.Head.Partition()})
]

Compile_output_partitions_v2 = [
    buffer_pb2.Buffer.Head.Partition(index={1 : buffer_pb2.Buffer.Head.Partition()}),
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
        })
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