#!/bin/bash
set -euo pipefail

if python3 -c "import grpc_tools.protoc" >/dev/null 2>&1; then
  python3 -m grpc_tools.protoc -I./protos --python_out=./protos --grpc_python_out=./protos ./protos/celaut.proto --experimental_allow_proto3_optional
  python3 -m grpc_tools.protoc -I./protos --python_out=./protos ./protos/pack.proto --experimental_allow_proto3_optional
else
  protoc -I./protos --python_out=./protos ./protos/celaut.proto --experimental_allow_proto3_optional
  protoc -I./protos --python_out=./protos ./protos/pack.proto --experimental_allow_proto3_optional
fi
