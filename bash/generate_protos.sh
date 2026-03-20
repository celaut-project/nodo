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
