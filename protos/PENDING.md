# Pending: the redesign of celaut.proto

This file records a redesign of `celaut.proto` that is not complete. It has the
decisions that the proto already shows, and the questions that are still open. The
code (Python, Rust TUI), the generated `*_pb2.py` files, the tests and `compat.json`
do not follow the proto yet. Remove this file when the last open question is closed
and the code follows the proto.

`protos/REASON.md` has the rules that the decisions follow. `protos/FABLE_PROPOSITION/`
has an independent analysis of the contract (`ANALYSIS.md`); the codes P1, R3.6, Q1.1
and the others below are the names of its proposals.

## Decided

### Quantities and resources

- `Quantity` (`tags`, `prose`, `formal`, `optional uint64 value`) replaces the typed
  fields of `Sysresources` and the `benchmark` key-value list. `Sysresources` and
  `Uint64KeyValue` are removed.
- Two classes of formal: **resource** (consumed: reserved, added, enforced, charged)
  and **benchmark** (measured: compared one by one, never added). A node refuses a
  formal in a list of the wrong class.
- One comparison rule: greater than or equal. A `Quantity` is only for such a thing.
  An exact value, a relative weight or a feature is a `Policy` (REASON 3.3). The old
  block I/O weight is removed: no node used it.
- `Container.Resources` has `at_init`, `at_most` (resources) and `benchmarks`
  (minimum scores). The stability of a score is a formal of a percentile.
- `Peer.rates` is a list of `Rate`, `Peer.limits` is a list of `Limit`. Their `scope`
  is bytes, compared exactly. `Slot.mu_per_call` becomes `Slot.rates` (what a
  service charges its callers; a node does not read it).
- The contract never names one resource (Q7). A node that cannot change a resource
  of a running instance refuses the request and names the formal of the resource.
- CPU is one formal, in thousandths of a core, total (Q5). The old rule "use the score
  of the smallest larger working set" is removed: a node that did not measure a
  working set refuses, or measures it.
- Benchmarks over time (Q6): a node measures at its start and then at a period that
  the operator sets, and announces a percentile only with enough measures. At a wake
  below a minimum, the instance stays hibernated. Low priority for nodo (REASON 1.7).

### Hibernation

- Hibernation is elemental (REASON 1.5): an instance has a process, or it has none.
  "Suspended" and "hibernated" are one state. The proto describes it once, in the
  comment "The life of an instance".
- A hibernated instance keeps its identity and its filesystem. The node reserves none
  of its resources. Its resources keep their values for the wake.
- `ModifyServiceSystemResourcesInput.hibernated` asks for the state. No value of a
  resource hibernates or wakes an instance.
- The default way to continue is a new process from the entrypoint (Q1.2). A service
  that needs another way declares an `Init` policy.
- How long a node keeps a hibernated instance that cannot pay is a promise of the
  offer (Q1.1). With no such promise, the caller assumes none.
- A wake starts a process, so it has a "start" receipt (Q1.3), with the same salt
  for the environment as the first start. `BalanceStatement.receipts` gives the
  start receipts of the processes in the range of the statement, so the party that
  pays gets also the receipt of a wake that nobody asked for. The movement of each
  start and wake names its receipt by id.
- A node that cannot hibernate an instance refuses and gives the reason (Q1.4). The
  network-share rule is a limit of nodo, not of the contract.
- The rate of the kept state is a `Rate` whose formal defines its base (Q1.5).

### Signatures and compatibility

- **`Signed` envelope (P1).** A node signs the exact bytes of a serialized message:
  `Signed { Body { context, signer, scheme, content }, signature }`. It replaces the
  signature fields of the signed messages and the digest keys of the layer formal.
  REASON 6.2.
- The signer (public key) is the bytes that the scheme defines, with no length in
  the contract (Q9.2). The id of a peer is the lowercase hexadecimal of those bytes.
- The signature scheme is one descriptor with one formal (P4).
- No `float` and no signed integer in signed or hashed messages; times, periods and
  ports are unsigned (R6.1, P10).
- A field that holds a hash is a `Metadata.HashTag.Hash`; the contract never names a
  hash algorithm (X-HASH, REASON 6.4). Nodo keeps blake2b-256.
- A node refuses a service with a field number that it does not know (R3.6).
- No field-number discipline across variants (R4.7 is not adopted; REASON 1.8).
- No `reserved` lines; the field numbers of each message are compact (REASON 4.5).
- `Container.node_protocol_stack` declares only the RPCs of the node that the service
  uses, so the id of a service does not change with each release of nodo (S6.5,
  issue #648).
- An address is a tree of layers (REASON 2.7): `Uri.layers` and `Slot.layers` replace
  the transport and the linear stack. A requirement (`Network.protocol_stack`,
  `Container.node_protocol_stack`) stays a path. Layers are compared by exact bytes by
  default; a new version is a sibling. A party can read two formals and accept a
  subset; nodo does it only to judge `node_protocol_stack` against its own gateway
  layer (REASON 2.5). This replaces R2.5 of the analysis (item by item).
- Chat and Observe left `celaut.proto` (P8, REASON 2.8). They are normal gRPC services
  in `protos/extensions/chat.proto` (`celaut.chat.Chat`: `Send`, `GetThread`) and
  `protos/extensions/observe.proto` (`celaut.observe.Observe`: `Watch`), each a branch
  of the gRPC layer of the address. `GetThread` now carries the `Client` in its
  request, because there are no bee-rpc indices.
- `compat.json` stays as it is: it only keeps nodo, the superapp and the library on
  the same version. Nodes compare the layer formal, not `compat.json`.

### Shape of the contract

- Offers and the Peer have two lists of terms: `conditions` (bind the caller; a
  caller refuses an unknown one) and `promises` (bind the node; a caller ignores an
  unknown one). The pricing fields of `EstimatedCost`, `OnClose`, `closes_at` and
  `ActivityWindow` become terms (P2, REASON 3.2). The maximum scarcity multiplier is a
  promise (Q2).
- No catalogs of formals in the comments of the proto (P3).
- One `Descriptor` replaces the eight copies of (tags, prose, formal) (P4).
- Removed: `Metadata.format`, `Metadata.reputation_proofs`,
  `Container.config_declaration` (the node writes `/__config__`). Kept:
  `Api.payment_contracts` (P5).
- `Policy` lists on `Network`, `Configuration` and `Device`; `facts` on the start
  receipt (P6).
- Merged: `Ack`, `Uri`, `TunnelRequest`, `InstanceState`, and a partial `Container`
  for `GetResourceAvailability` (P7).
- `NoticeDeclaration` is an `Init` policy (P9).
- `ResourceAvailability.unknown_formals`, and the formals that a node does not know in
  the error details of `StartService` and `GetServiceEstimatedCost` (P10, REASON 3.7).
- The formal files go in `protos/formals/<class>/` (P3, Q4). Done for the files that
  existed: `protos/formals/policies/` and `protos/formals/payment_protocols/`. Still to
  do (open question 7): a `class=` line in each formal, the exact bytes of the file (no
  final newline), and a generated index of ids. The `class=` line changes the bytes of
  the formals of today.

### Dependencies

- `Dependency.is_completed` is removed (open question 1). The hash field names the
  service of a workload. The service field is complete only if its hash, with the
  algorithm of a type in the hash field, is equal to the value of that type. A flag
  could say "complete" for a service with another hash. A dependency with no hash
  names no service, so the packer must write the hash of a full service.
- `Dependency.on_filesystem` becomes `Dependency.sources`: a list of `Policy` in an
  announcement. Where a party gets a service (the filesystem of the parent, a
  registry, the network) is an open set, so each kind of source is a formal. A node
  that does not know a source ignores it.
- Known cost: the sources are in the id of the parent. If the location of a child
  changes, the parent gets a new id. A source does not change which service the hash
  names.

## Open questions

Each question keeps its number when it closes. Question 1 (the dependency of a
service) is closed: see "Dependencies".

### 2. Network (X-NET)

A network is not only DNS: there are also peer-to-peer and proof-of-work networks
(for example the network that the Laravel instances of `copa-ai/personal-nutrition`
use to reach Postgres). Today the data (a host name) is in the tags, and the identity
is by tag (against REASON 2.2). A possible form: a descriptor of the kind of network
plus its value. Read that project before a proposal.

### 3. Split of ExecutionReceipt (X-REC)

16 of its 27 fields say "only in ...". A split into a start receipt and a checkpoint
follows REASON 4.2. Not done, to keep the risk low.

### 4. Messages that do not come from their writer (REASON 1.8)

Between two nodes, the layer formal tells the schema of each message. Two cases have
no announced schema: a service that a third node forwards (compatibility of schemas is
not transitive) and a service in a file. Known residual risk. No change for now.

### 5. Devices against PR #649 (P6)

`Device.policies` replaces the enum `Attach`, and `Peer.devices` changes. Check it
against the draft pull request #649 before the code follows.

### 6. The binding "this service implements this formal" (REASON 1.6)

The trust rule is decided. The mechanism (how a service declares the formal that it
implements, how a node finds it, one API for each kind of function) is a future idea,
not necessarily through `GetReputationProofs` (Q9.4 not adopted). Who vouches for the
first verifier is also open.

### 7. The core formals to write

Part 1 is done: the formal files that existed are in `protos/formals/policies/` and
`protos/formals/payment_protocols/`, with no change to their bytes. Part 2 is this
list. Each formal goes in `protos/formals/<class>/`, and a test pins its bytes to the
bytes in the code of nodo.

The format, for all the formals:

- A `class=` line (resource, benchmark, rate, limit, policy, condition, promise,
  ...), so that "a formal in a list of the wrong class" can be checked from the
  bytes. This line changes the bytes of the formals of today.
- The exact bytes are the bytes of the file, with no newline at the end.
- A generated index with the id of each file (`<type>:<digest>`, REASON 6.3).
- No path and no name of this implementation in a formal.

Formals that exist today only in the code of nodo, with no file:

- The signature scheme (one descriptor, P4).
- The ledgers (Ergo, Bitcoin).
- The transports and the layers of an address (tcp, tls, http2, grpc, bee-rpc). The
  layer `celaut-gateway` is a wire schema (REASON 2.5): split its rules that must be
  equal into formals of their own.
- The templates of the proof-of-work networks.
- The architectures (today the packer writes only tags, against REASON 2.2).

New formals of the redesign:

- Resources: memory, disk, CPU (thousandths of a core), device memory.
- Benchmarks: the current keys as means (`int_ops_per_sec`, `flt_ops_per_sec`,
  `sha256_hashes_per_sec`, the memory bandwidth for each working set), and the
  percentiles to support.
- Rates: the kept state of a hibernated instance, and one for each old key
  (`ram_mu_per_gib_second`, `cpu_mu_per_vcpu_second`, `disk_mu_per_gib_second`,
  `net_mu_per_gib`, `build_mu`, `tunnel_open_mu`, `connection_open_mu`,
  `modify_resources_mu`, `statement_query_mu`, `statement_row_mu`).
- Limits: the network limits of today, and a "global" formal for each.
- Terms: the price bound, the maximum scarcity multiplier, the schedule and what the
  node does at its close, the grace period of a hibernated instance.
- Policies: the notice channel (`NoticeDeclaration`, P9), the way to continue after a
  hibernation (Q1.2), the ways to attach a device (P6), and the RPCs of the node that
  a service uses (`node_protocol_stack`, S6.5).
- Sources of a dependency (`Dependency.sources`): a file in the filesystem of the
  service, and a registry.

## Implementation stages

The contract is complete before nodo implements all of it (REASON 1.7).

1. Proto (draft done), and the core formals in `protos/formals/`.
2. The `Signed` envelope in nodo, the TUI and the Peer on the ledger. Remove the
   digest keys from the layer formal.
3. The tree of layers: announce it, find a path in it by exact bytes, and the subset
   check of `node_protocol_stack`. Move Chat and Observe to their own gRPC services.
4. Terms: pricing, schedule, receipts, balance movements.
5. Resources: cgroups and the virtual machine from the formals.
6. Placement: availability, benchmarks, devices.
7. CLI, TUI, docs, `compat.json`.
