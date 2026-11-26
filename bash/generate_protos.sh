#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# contract/ holds the specification (celaut.proto, its extensions, buffer.proto from
# bee-rpc). protos/ is the Python package of nodo: the generated code goes there, with the
# formats of nodo only (pack.proto, invite.proto).
CONTRACT_DIR="${ROOT_DIR}/contract"
PROTO_DIR="${ROOT_DIR}/protos"

# contract/buffer.proto is compiled for the Rust TUI (src/commands/tui/build.rs)
# and is kept on disk so celaut.proto's `import "buffer.proto";` resolves here,
# but it is deliberately NOT passed to --python_out / --grpc_python_out below:
# bee-rpc-over-grpc-py vendors its own compiled buffer_pb2, and protos/buffer_pb2.py
# is a hand-written shim that aliases to it (see that file). Generating a second,
# independent buffer_pb2.py here would make Python's protobuf descriptor pool
# reject one of the two with "duplicate file name buffer.proto" as soon as both
# got imported into the same process.
# protos/celaut_doc.binpb is celaut.proto's descriptor with its comments
# (--include_source_info). The gencode drops the comments, and the node has no protoc at
# run time. The prose of the celaut-gateway protocol layer is built from these comments
# (src/identity/protocol_doc.py), so the comments are the only copy of that text.
if python3 -c "import grpc_tools.protoc" >/dev/null 2>&1; then
  python3 -m grpc_tools.protoc \
    -I"${CONTRACT_DIR}" \
    --python_out="${PROTO_DIR}" \
    --grpc_python_out="${PROTO_DIR}" \
    --include_source_info \
    --descriptor_set_out="${PROTO_DIR}/celaut_doc.binpb" \
    "${CONTRACT_DIR}/celaut.proto" \
    --experimental_allow_proto3_optional

  python3 -m grpc_tools.protoc \
    -I"${PROTO_DIR}" \
    -I"${CONTRACT_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/pack.proto" \
    --experimental_allow_proto3_optional

  python3 -m grpc_tools.protoc \
    -I"${PROTO_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/invite.proto"
else
  protoc \
    -I"${CONTRACT_DIR}" \
    --python_out="${PROTO_DIR}" \
    --include_source_info \
    --descriptor_set_out="${PROTO_DIR}/celaut_doc.binpb" \
    "${CONTRACT_DIR}/celaut.proto" \
    --experimental_allow_proto3_optional

  protoc \
    -I"${PROTO_DIR}" \
    -I"${CONTRACT_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/pack.proto" \
    --experimental_allow_proto3_optional

  protoc \
    -I"${PROTO_DIR}" \
    --python_out="${PROTO_DIR}" \
    "${PROTO_DIR}/invite.proto"
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
