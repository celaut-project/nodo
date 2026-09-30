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

## Key/value lists (no `map<>` fields)

`xattrs`, `mu_per_call`, `environment_variables` and `min_benchmark` are
`repeated <Value>KeyValue` messages, not protobuf maps. protobuf leaves the order of map
entries on the wire to the implementation, and a service id is a hash of the serialized
specification, so the same service could serialize to two byte strings — and get two ids —
depending on the protobuf implementation. A repeated field keeps its order.

| Entry message | `value` | Used by |
|---|---|---|
| `BytesKeyValue` | `bytes` (`optional`) | every `xattrs` (`Contract`, `ItemBranch`, `Filesystem`, `Init`, and `pack.Service.Container.Init`), `Configuration.environment_variables` |
| `AmountKeyValue` | `Amount` | `Peer.mu_per_call`, `Service.Api.Slot.mu_per_call` |
| `DataFormatKeyValue` | `DataFormat` | `Service.Container.environment_variables` |
| `Uint64KeyValue` | `uint64` (`optional`) | `Sysresources.min_benchmark` |

Every field kept its number and name. `buffer.proto`'s `map<int32, Partition>` is
bee-rpc's and is untouched.

**Wire compatibility.** A proto3 `map<K, V> f = N` is encoded exactly as
`repeated Entry { K key = 1; V value = 2; } f = N`, so a node built before this change and
one built after read each other's messages, and a service already stored still parses and
re-serializes to the same bytes (so its id does not move). `value` is `optional` on the
scalar entry types on purpose: without it proto3 leaves out a `value` that is empty or 0,
where a map entry always wrote both fields, and those bytes would differ.
`tests/test_keyvalue_wire.py` checks both directions for every converted field.

**Canonical order.** What a node *builds* is written by `src/utils/keyvalue.py`:
entries sorted by key (UTF-8 bytes), one entry per key, no empty key. The packer, the
advertised rates, the instance environment and the filesystem xattrs all go through it, so
the same content is the same bytes whatever order it was gathered in. A repeated key is a
packing error in `service.json`.

**Reading.** What a node *receives* is never reordered or de-duplicated by being read: it
is forwarded as it arrived. A key resolves to the LAST entry carrying it, which is what
parsing a map did. Use `keyvalue.to_dict` / `keyvalue.get`, never a hand-written loop.

**JSON.** `service.json` keeps its object syntax (`"mu_per_call": {"Solve": 100}`); the
packer turns it into the sorted entry list. The Peer JSON published on-chain
(`keyvalue.message_to_json`) keeps the object shape too. protobuf's own
`MessageToDict` / `ParseDict` now show these fields as a list of `{key, value}`.

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
`-I` so `celaut.proto`'s import resolves. Generate with `grpcio-tools==1.56.0`
(protoc 23.1): the committed `celaut_pb2.py` is that output with the `buffer_pb2`
import pointed at `bee_rpc`.

## Operational notes

- This migration is breaking: no legacy fallback.
- Packers fail explicitly if they receive legacy keys (`entrypoint`, `config`, `resources.start_time_ms`).
