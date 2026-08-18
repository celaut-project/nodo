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

## Codegen

`bash/generate_protos.sh` now supports two modes:

1. `grpc_tools.protoc` (if available)
2. Native `protoc` (fallback for generating `*_pb2.py`)

## Operational notes

- This migration is breaking: no legacy fallback.
- Packers fail explicitly if they receive legacy keys (`entrypoint`, `config`, `resources.start_time_ms`).
