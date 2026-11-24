# CELAUT Protos Hard Cutover Migration

Status: **implemented in protobuf schema** and adapted in the main Python consumers.

## Current schema

- `Contract`
  - `ledger = 1`
  - `protocol = 2` (`Contract.Protocol`: `tags`, `prose`, `formal`): the kind of payment
    contract. Its `formal` defines the keys of `xattrs`. It replaces the xattr
    `contract_type` (#671). See `docs/payment_protocols`.
  - `xattrs = 3` (`script`, `address`, `token_id`, and on a reputation proof
    `owner_public_key` and `owner_signature`)
- `Service.Container`
  - `init` (`entry_path`, `policies`)
  - `config_declaration`
- `Service.Api.Slot`
  - `transport`
  - `mu_per_call`
- `Service.Network`
  - `protocol_stack`
- `Resources.start_time_ms` removed.

## Per-architecture resources (#459)

- `ArchitectureResources { Service.Container.Architecture architecture = 1;
  Sysresources resources = 2; }` — a resource shape for one architecture. A single
  `Sysresources`, with no `at_init`/`at_most` split: the message only says what is
  supported or asked about.
- `Peer.resources = 9` is `repeated ArchitectureResources`: the most a node supports per
  architecture it serves, which doubles as its list of supported architectures.
  `resources` carries capacity ceilings and the node's measured `benchmark` scores. It is
  covered by the `Peer` signature (`node_identity.canonical_peer_content_digest`).
- `Peer.ts` is field 3 (see "Order of the fields of `Peer`").
- `GetResourceAvailability` takes `Client & ArchitectureResources` (it took a bare
  `Service.Container.Resources`). An empty `architecture` means the answering node's
  native one. In a request, the limits are the most an instance may grow to (the former
  `at_most`) and `benchmark` is the minimum required (the former `at_init.benchmark`).
- `Sysresources.min_benchmark` (field 6) is now `Sysresources.benchmark`. Same number,
  so the same bytes; what it means depends on where it sits (see its comment).

**Not wire compatible**, deliberately: every node is upgraded together. A node from
before this change reads field 8 of a new `Peer` as the wrong type, and a new node
reading a `Peer` stored before it (`peer.advertisement`) finds no `resources` there,
which the peer pre-filter treats as "announced nothing" and calls as before.

## Devices with a direct connection (#536)

The wire format for devices (a GPU, for example) is in `celaut.proto`. A node does not
attach devices yet: this is only the protocol, so a node that is released later does not
break compatibility.

- `Device { Container.Architecture architecture = 1; Container.Resources resources = 2;
  repeated Attach attach = 3; }`
  and `Container.devices = 9` (a service repeats an entry for each device it needs).
  A device has one architecture. Its tags are synonyms, never alternatives.
  `resources` is a `Container.Resources`, with `at_init` and `at_most` like the main one.
- `Device.attach = 3` (a list of `ATTACH_AT_START` and `ATTACH_ON_REQUEST`; empty is
  `ATTACH_AT_START`),
  `ModifyServiceSystemResourcesInput.devices = 4` (a `DeviceChange` with `attach` and
  `detach` by id) and `ModifyServiceSystemResourcesOutput.devices = 3` (a list of
  `AttachedDevice`, with the id and the guest PCI address) are reserved for a
  device that an instance asks for during its run. A node today supports only
  `ATTACH_AT_START`. It refuses a service with `ATTACH_ON_REQUEST` and a request that
  sets `devices`, and never ignores them.
- `ConfigurationFile.devices = 5` is a list of `AttachedDevice`: the devices that the
  node attached at the start, with their ids and guest PCI addresses. The service reads
  them in `__config__`, as it reads its networks.
- `Peer.devices = 10` (unchanged) is `repeated Device`. An entry repeated is a device
  repeated, so the number of entries of one architecture is the count. Only `at_most`
  has a value (the limits and the measured benchmark), and `attach` lists all the ways
  that the node can attach that device. It is covered by the `Peer` signature.
- `GetResourceAvailability` takes `Client & ResourceAvailabilityRequest { main = 1;
  devices = 2; }`, where `devices` is a list of `Device` (it took `Client & ArchitectureResources`).

**Not wire compatible** with the request of #459, deliberately (alpha).

## Key/value lists (no `map<>` fields)

`xattrs`, `mu_per_call`, `environment_variables` and `benchmark` are
`repeated <Value>KeyValue` messages, not protobuf maps. protobuf leaves the order of map
entries on the wire to the implementation, and a service id is a hash of the serialized
specification, so the same service could serialize to two byte strings — and get two ids —
depending on the protobuf implementation. A repeated field keeps its order.

| Entry message | `value` | Used by |
|---|---|---|
| `BytesKeyValue` | `bytes` (`optional`) | `Contract.xattrs` and `ItemBranch.xattrs`, `Configuration.environment_variables` |
| `AmountKeyValue` | `Amount` | `Peer.rates`, `Service.Api.Slot.mu_per_call` |
| `DataFormatKeyValue` | `DataFormat` | `Service.Container.environment_variables` |
| `Uint64KeyValue` | `uint64` (`optional`) | `Sysresources.benchmark` |

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

## Comments are protocol text

The leading comment of each message, field and RPC in `celaut.proto` is its protocol
description, in ASD-STE100. The node puts it in the prose of the `celaut-gateway`
layer that it announces (`src/identity/protocol_doc.py`). A note for developers goes
in a detached comment, with a blank line between it and the element. The head of
`celaut.proto` gives the rules.

The gencode drops the comments, so `bash/generate_protos.sh` also writes
`protos/celaut_doc.binpb`: the descriptor with its source info. Run the script after
each change to `celaut.proto`, also a change to a comment only. If you only changed
comments, this command is sufficient, with any version of `grpcio-tools`:

```
python3 -m grpc_tools.protoc -Iprotos --include_source_info \
  --descriptor_set_out=protos/celaut_doc.binpb protos/celaut.proto \
  --experimental_allow_proto3_optional
```

`tests/identity/test_protocol_doc.py` fails if an element has no description, or if
`celaut_doc.binpb` is not current.

## The encoding of a message with an id

The `Service` and the `Filesystem` of its container have an id: the hash of their bytes. A
field that is added to them can only use the types that [the canonical
encoding](../docs/CANONICAL_ENCODING.md) allows, and a test fails if it does not
(`tests/test_canonical_proto.py`). A number of a field is never changed after a release.

## Order of the fields of `Peer`

The fields of `Peer` were numbered in the order that they were added, and `ts` was 99. Now
the numbers follow the groups of the message, and the declarations are in the same order:

| Group | Field | Number (was) |
|---|---|---|
| Identity and signature | `public_key` | 1 (5) |
| | `signature_scheme` | 2 (7) |
| | `ts` | 3 (99) |
| | `signature` | 4 (6) |
| Where the node is | `uri` | 5 (2) |
| What it charges and limits | `payment_contracts` | 6 (3) |
| | `rates` | 7 (4) |
| | `limits` | 8 (11) |
| What it can run | `resources` | 9 (8) |
| | `devices` | 10 (10) |
| | `activity_window` | 11 (9) |
| (removed) | `reputation_proofs` | 12 reserved (1) |

## The pointers to proofs on ledgers moved to `ReputationProof`

`Peer.reputation_proofs` (a `repeated Contract`) is removed: its number 12 and its name are
`reserved`. The same pointers are now `ReputationProof.ledger_proofs = 7`, in the proof that
the node signs with its identity key and gives with `GetReputationProofs`. One message now
holds everything that a node says about reputation, and the `Peer` holds only what a node
says about itself.

- A `Contract` in `ledger_proofs` names the ledger, the script and the token of one proof
  on a ledger. Its `xattrs` carry the owner attestation: the wallet that owns the proof
  vouches for the identity of the node.
- The digest of the `Peer` no longer has the proofs segment. The digest of the
  `ReputationProof` (`offchain_proof.pow_base`) has a new last segment: the sorted pointers,
  joined by `/`. The signature covers them, so a node that passes the proof on cannot add
  or remove one.
- A node learns the pointers of a peer when it pulls the proof of that peer (about every
  `reputation.OFFCHAIN_PULL_INTERVAL_SECONDS`), no more with the `Peer`. It checks each
  attestation when it keeps the proof (`offchain_proof.check_ledger_proofs`). A failure is
  a log line: the opinions of the proof still count on their own.
- The `Peer` that a node publishes on a ledger has no pointers, so the attestation is no
  more on the ledger with it.

## Order of the fields of `ReputationProof`

The same rule as `Peer`: the fields that make the envelope of the signature come first, and
the content comes after them.

| Group | Field | Number (was) |
|---|---|---|
| Identity and signature | `publisher` | 1 (1) |
| | `signature_scheme` | 2 (4) |
| | `ts` | 3 (2) |
| | `pow_nonce` | 4 (5) |
| | `signature` | 5 (6) |
| Content | `opinions` | 6 (3) |
| | `ledger_proofs` | 7 (7) |

## Order of the fields of `ExecutionReceipt`, `BalanceStatement` and `EstimatedCost`

The same rule as `Peer`: the envelope of the signature first, then the content by topic.
The fields that a kind of message uses are together.

`ExecutionReceipt`: the envelope (`kind` 1, `peer_id` 2, `signature_scheme` 3, `ts` 4,
`signature` 5), the execution (`execution_name` 6), the fields only in "start"
(`service_id` 7 to `devices` 16: the service, the environment, the funding and what the node
reserved), the fields only in "checkpoint" and "close" (`plane` 17 to `coverage` 28), and the
fields only for Observe (`capture_mode` 29, `degraded_reason` 30).

`BalanceStatement`: the envelope (`ts` 1, `signature` 2, `relayed` 3), which account
(`account_ref` 4, `service_id` 5, `instance_ref` 6), the range asked for (`from_seq` 7,
`to_seq` 8, `movements` 9), and the state of the account (`balance` 10, `head_seq` 11,
`head_hash` 12, `oldest_seq` 13, `retention_days` 14).

`EstimatedCost`: the price (`cost` 1 to `launch_scarcity` 5), the promise on the price
(`price_bounded` 6, `price_volatility` 7, `volatility_period_seconds` 8), the time of the
offer (`valid_until` 9, `closes_at` 10, `on_close` 11), and `signature` 12.

The digest of an `ExecutionReceipt` follows the new numbers (`execution_receipt.canonical_receipt`
and `receipt.digest` in `transport_stack`): `signature_scheme` is now the third part of the
text, in place of the last, and the fields only in "start" come before the stream fields. A
receipt signed before this change does not verify.

The digests of `BalanceStatement` and `EstimatedCost` follow the numbers too
(`statement_signature.statement_digest` and `signed_offer.offer_digest`). In the statement,
`relayed` is now always in the text, as 1 or 0, and `service_id` and `instance_ref` have
their own place and are empty when they have no value: before, each one was added at the
end only when it had a value. A statement or an offer signed before this change does not
verify.

The digests of `Peer` and `ReputationProof` follow the numbers too:

- `Peer` (`node_identity.canonical_peer_content_digest`): `signature_scheme`, `uri`,
  `payment_contracts`, `rates`, `limits`, `resources`, `devices`, `activity_window`. Before,
  the text was `uri|payment_contracts|rates|limits|scheme|resources|activity_window|devices`.
  `public_key` and `ts` are not in the digest. The signed text is still
  `<public_key>|<ts>|<digest>`, so the key and the time come before the digest.
- `ReputationProof` (`offchain_proof.pow_base`): `publisher|signature_scheme|ts|opinions|
  ledger_proofs`. `pow_nonce` (field 4) is not in the place of its number: `proof_digest` is
  the hash of `base|pow_nonce`, so a node that looks for a nonce hashes the base once and
  then only these last bytes, for each try. Moving the nonce into the text would make each
  try hash all the opinions.

A `Peer` or a proof signed before this change does not verify. The formal text of
`peer.digest` was out of date (it had the proofs and lacked `limits`, `devices` and
`activity_window`), and it now lists the parts with their encodings.
`tests/test_digests_follow_field_numbers.py` writes the four texts by hand.

**Not wire compatible**, as the other changes of the alpha. A receipt or a statement that a
client or a node kept before is unreadable. The fault evidence that a node kept
(`FaultEvidence` holds statements and receipts) is skipped, until new evidence comes.

**Not wire compatible**, as the other changes of the alpha. A proof that a node kept before
(`offchain_proofs`) is unreadable and is skipped, until the publisher gives a newer one. The
signature does not change: the digest is made from the fields, not from their numbers.

**Not wire compatible**, as the other changes of the alpha: every node is upgraded
together. A node reads a `Peer` that it stored before as unreadable or empty (the readers
of `peer.advertisement` already treat that as "nothing announced"), until the peer
announces itself again. The signature of a `Peer` changes, because its digest changed
twice: it now has the segment `limits` (superapp must add it) and no proofs segment. A
client that checks it needs the new `celaut.proto` and the new digest.

## Operational notes

- This migration is breaking: no legacy fallback.
- Packers fail explicitly if they receive legacy keys (`entrypoint`, `config`, `resources.start_time_ms`).
