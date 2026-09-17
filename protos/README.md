# CELAUT Protos Hard Cutover Migration

Status: **implemented in protobuf schema** and adapted in the main Python consumers.

## Current schema

- `Contract`
  - `ledger = 1`
  - `xattrs = 2` (`script`, `address`, `token_id`, optional `reputation_key`)
- `Service.Container`
  - `init` (`entry_path`, `xattrs`)
  - `config_declaration`
- `Service.Api.Slot`
  - `transport`
  - `mu_per_call`
- `Service.Network`
  - `protocol_stack`
- `Resources.start_time_ms` removed.

## Schema files

- `protos/celaut.proto`
- `protos/pack.proto`
- `protos/buffer.proto`

These are the only copies. The Rust TUI used to vendor its own `celaut.proto` and
`buffer.proto` under `src/commands/tui/protos/`; that copy drifted until
`Service.Api.slot` was field 4 there and field 1 here — incompatible on the wire,
while this file claimed they were identical. `src/commands/tui/build.rs` now compiles
this directory directly, so there is nothing left to keep in sync.

`protos/buffer.proto` is a second exception, of a different kind: it is also
vendored upstream by the `bee-rpc-over-grpc-py` dependency (pinned in
`bash/requirements.txt`), which ships its own compiled `bee_rpc.buffer_pb2`.
`protos/buffer.proto` stays on disk here only because `celaut.proto`'s
`import "buffer.proto";` and the Rust TUI build need a local file to read — but
at Python runtime there is exactly one compiled `buffer_pb2` (`bee_rpc`'s);
`protos/buffer_pb2.py` is a hand-written shim that aliases to it (see that
file's header). Keep `protos/buffer.proto` byte-for-byte in sync with
`bee-rpc-over-grpc-py`'s own `src/bee_rpc/buffer.proto` whenever that pin is
bumped — if the two drift, Rust and Python nodes end up assuming different
wire shapes for the same message.

## Codegen

`bash/generate_protos.sh` now supports two modes:

1. `grpc_tools.protoc` (if available)
2. Native `protoc` (fallback for generating `*_pb2.py`)

It only compiles `celaut.proto` and `pack.proto` to Python — never
`buffer.proto` (see the shim note above). `buffer.proto` is still passed via
`-I` so `celaut.proto`'s import resolves.

## Operational notes

- This migration is breaking: no legacy fallback.
- Packers fail explicitly if they receive legacy keys (`entrypoint`, `config`, `resources.start_time_ms`).
