#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROTO_DIR="${ROOT_DIR}/protos"

if python3 -c "import grpc_tools.protoc" >/dev/null 2>&1; then
  python3 -m grpc_tools.protoc \
    -I"${PROTO_DIR}" \
    --python_out="${PROTO_DIR}" \
    --grpc_python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/buffer.proto" \
    "${PROTO_DIR}/celaut.proto" \
    --experimental_allow_proto3_optional

  python3 -m grpc_tools.protoc \
    -I"${PROTO_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/pack.proto" \
    --experimental_allow_proto3_optional
else
  protoc \
    -I"${PROTO_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/buffer.proto" \
    "${PROTO_DIR}/celaut.proto" \
    --experimental_allow_proto3_optional

  protoc \
    -I"${PROTO_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/pack.proto" \
    --experimental_allow_proto3_optional
fi

# The node's protobuf runtime is pinned to 4.23.3 by bee-rpc, so the gencode has to
# stay in the 4.x line. A protoc from the 5.x line onwards emits a
# `runtime_version.ValidateProtobufRuntimeVersion` call that the pinned runtime does
# not even export, and the node dies on import. Catch it here instead of at install
# time: generate with grpcio-tools==1.56.0.
if grep -l "runtime_version" "${PROTO_DIR}"/*_pb2.py >/dev/null 2>&1; then
  echo "Error: the generated code targets a protobuf runtime newer than the pinned 4.23.3." >&2
  echo "Regenerate with 'pip install grpcio-tools==1.56.0' in scope." >&2
  exit 1
fi
