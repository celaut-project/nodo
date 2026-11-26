# Pending: the redesign of celaut.proto

This file records what is still to do for the redesign of `celaut.proto`. The proto is
the reference for what is decided: its comments give each rule, and `contract/REASON.md`
gives the reasons. The history of each decision is in git. The code (Python, Rust TUI),
the generated `*_pb2.py` files, the tests and `compat.json` do not follow the proto
yet. Remove this file when the code follows the proto.

## 1. Decisions that are still open

- **The format of a schema** (`contract/descriptors/schemas/`). Today
  `src/identity/protocol_schema.py` gives `schema.<name>=<num>:<cardinality>:<type>,...`
  with no names of fields. A schema must also give the name of each field: without
  them, two variants that add one field number with the same type have the same
  schema. Decide the format before any schema file. The layer `celaut-gateway` uses
  the same format.
- **`config-file`.** The proto removed `Container.config_declaration`, and the node
  always writes `/__config__`. Decide if a setting of `Init` is necessary, or if the
  path is a part of the architecture.
- **The Ergo reputation ledger.** Its formal holds the R4 type ids of the
  configuration of the node (`src/utils/ledger_descriptors.py:184-224`). Decide if the
  ids are a part of the ledger, or values.
- **The ledger tag `bitcoin` as a network with no addresses**
  (`src/manager/networks.py:149`). Decide if it is still necessary.

## 2. The descriptors to write

The rules are in `contract/descriptors/REASON.md` and in the `REASON.md` of each folder.
The list of the descriptors that nodo needs, with where nodo implements each one
today, is the second part of `contract/descriptors/INDEX.md`. It is the work for the
agents that write the descriptors.

## 3. Facts of nodo today that the implementation must change

The inventory of the code (November 2026) found these. Each one is a change of the
code, not of the contract.

- **Formal bytes.** Outside the layers of an address, the ledgers, the signature
  scheme and the proof of work, the code has formal bytes only for the six settings of
  `src/utils/policies.py`. The architectures, the resources, the benchmarks, the
  rates, the terms, the networks and the facts are tags, keys of `service.json` or
  keys of the configuration.
- **The eight descriptor files of today** (`policies/` and `contracts/payment/`) need
  the lines `class=` and `name=`. Their bytes are on the wire today, so this change
  goes with the code. `policies.make` must then read the tags and the prose from the
  file, not make them.
- **`scarcity_max_multiplier` is sent in `Peer.rates`.** It is a promise, not a
  price.
- **The announced rates are truncated.** The configuration is in MU for each hour,
  the announcement is in MU for each second (`hour // 3600`), and the charge uses the
  price for each hour. Select units for the rate formals that give exact integers.
- **`Peer.limits` is never written.** nodo enforces its limits with keys of its
  configuration only. A caller cannot know them.
- **The facts `kernel` and `devices` of the start receipt are always empty**
  (`src/gateway/launcher/local_execution/local_execution.py:614`).
- **`node_protocol_stack`.** The node checks only the tag `celaut-gateway`
  (`src/virtualizers/microvm/rootfs.py:445-459`). The subset check is a TODO there.
  The packer writes the full gateway stack when `service.json` has `uses_gateway`.
- **Architectures** are matched by tag (`src/virtualizers/architecture.py:45-65`),
  and the configuration flag `TRUST_METADATA_ARCHITECTURE` reads tags from `Metadata`,
  which the proto removed.
- **Benchmarks** are one run at the start of the node, with no statistic. The rule
  "use the score of the next larger working set" is removed: a node that did not
  measure a working set refuses, or measures it. A node measures again at a period
  that the operator sets, and announces a percentile only with enough measures (low
  priority, REASON 1.7).
- **CPU** is two numbers today (`cpu_quota`, `cpu_period`). The formal is one
  integer: thousandths of a core.
- **PR #649** (devices with VFIO, attach at the start only). Its tags
  `pci:<vendor>:<device>+<alg>:<hash>` become an architecture formal (the family that
  the software needs), a model fact, and the setting of the guest kernel. The price
  and the receipt read the model fact. A device benchmark that the node did not
  measure refuses. The receipt fields of its proto diff become `facts`.
- **`TunnelRequest.slot`** is still a decimal text: it is not signed.

## 4. Known state of the branch

- `celaut_pb2.py` is not generated from the new proto, so the code builds the old
  messages.
- `tests/identity/test_protocol_doc.py` fails: `celaut_doc.binpb` is not generated
  from the new proto.
- Ten other tests fail on this branch for reasons outside this work (install, help,
  pricing, observe, update manifest).

## 5. Implementation stages

The contract is complete before nodo implements all of it (REASON 1.7).

1. Proto (draft done), and the core descriptors in `contract/descriptors/`.
2. The `Signed` envelope in nodo, the TUI and the Peer on the ledger. Remove the
   digest keys from the layer formal.
3. The tree of layers: announce it, find a path in it by exact bytes, and the subset
   check of `node_protocol_stack`. Move Chat and Observe to their own gRPC services.
4. Terms: pricing, schedule, receipts, balance movements.
5. Resources: cgroups and the virtual machine from the formals.
6. Placement: availability, benchmarks, devices.
7. CLI, TUI, docs, `compat.json`, and the `class=` and `name=` lines of the eight
   descriptor files of today.
