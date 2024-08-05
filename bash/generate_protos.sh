#!/bin/bash

# celaut
python3 -m grpc_tools.protoc -I./protos --python_out=./protos ./protos/celaut.proto --experimental_allow_proto3_optional &&

# compile
python3 -m grpc_tools.protoc -I./protos --python_out=./protos ./protos/compile.proto --experimental_allow_proto3_optional &&

# gateway
python3 -m grpc_tools.protoc -I./protos --python_out=./protos --grpc_python_out=./protos ./protos/gateway.proto --experimental_allow_proto3_optional
