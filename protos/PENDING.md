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
- A field that holds a hash is a `Hash`; the contract never names a
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

### Networks

- `Service.Network` is `{ Policy domain, protocol_stack, policies }` (open question
  2, X-NET). The formal of the domain is the kind of network: what a member is, how a
  node finds the members, and which addresses the instance can reach. The value of
  the domain gives its parameters (a host name and its ports, the identity of a shared
  database, the block of a chain). Today the node knows four kinds: all addresses
  (the tag `*`), host names, domains of celaut instances (the Postgres of
  `copa-ai/personal-nutrition`), and proof-of-work networks.
- Two domains are the same only if the formals and the values have the same bytes,
  after the node applies the settings of the network. A node never compares by tag
  (REASON 2.2). Nodo can read a value to accept more keys (REASON 2.5).
- The `${VAR}` template is a setting in `Network.policies`. The node fills the value
  from `Configuration.environment_variables` at the start. The id of the service keeps
  the template. `ResolveNetwork` refuses a network with a setting that changes the
  value at a start.
- `NetworkResolution` holds the `Network` as the service declares it, and not its
  tags. The instance finds its network by the bytes.
- `ShareLocationRequest.share_id` is the id that the formal of the share setting
  defines. The contract does not fix the hash `H("network", tag, ...)`.
- Known cost: two services that do not agree on the bytes of a domain do not meet.
  A common tag is no longer enough.

### Receipts

- `ExecutionReceipt` is split (open question 3, X-REC, REASON 4.2): `StartReceipt`
  for each process, and `TunnelCheckpoint` for each part of a `ServiceTunnel` stream.
  Each receipt has only the fields of its kind. `Signed.Body.context` gives the type,
  so the fields `kind` and `plane` are removed. A "close" is a checkpoint with
  `close` set.
- The two directions of a stream are each an `EventChain` (events, bytes, head).
- The checkpoint of the Observe stream is `ObserveCheckpoint` in
  `protos/extensions/observe.proto` (REASON 2.8). `coverage`, `capture_mode` and
  `degraded_reason` go there: a tunnel always sees all its bytes.
- The session is bytes, not hexadecimal text. The slot of a checkpoint is the internal
  port as `uint32` (R6.1). `TunnelRequest.slot` is still a decimal text: it is not
  signed.
- Known residual: `coverage` and `capture_mode` of the Observe extension are texts of
  a closed list. The extension is optional, so they do not touch the contract.

### Schemas

- A message whose bytes can reach a reader that did not get them from the writer
  holds its schema (open question 4, REASON 1.8). The schema is a `Descriptor`: its
  formal gives the structure of the type and of the messages that it contains, with
  the name of each field.
- Only two places: `Service.schema` (field 1, the same number in every variant) and
  `Signed.Body.context` (a `Descriptor`, no longer the name of the type). `Service` is
  the only message whose id is a hash of its bytes. Every other message that a third
  party reads is signed (`Peer`, `ReputationProof`, receipts, statements, offers,
  checkpoints, chat entries, and the evidence that holds them).
- Why: a hash names bytes, not a meaning. Without a schema, a publisher can say that
  a service id that another publisher judged means something else.
- The schema is a formal like the others (REASON 2.2): a new meaning is a new schema.
  A comment does not change the schema, so it does not change the ids. A change to the
  structure of `Service` or of a message that it contains changes the id of every
  service.
- Rejected: the full descriptor of the file with its comments (up to 142 KB, and its
  bytes depend on the version of `protoc`), and a schema in every message.

### Hashes and names

- `Metadata` is removed, with `HashTag` and `AttrHashTag` (open question 8). `Hash`
  (type and value) is a message of the first level, for every hash of the contract:
  services, accounts, instances, chains of events, executions.
- `Hash` has no `Descriptor`, and the hash algorithms have no formal files. The type
  (the digest of the empty input) identifies the algorithm, and a node can check it.
  Rejected: a descriptor in each hash (one role in each place, REASON 3.5, and its
  cost in each chain and each id).
- A service travels as its hashes or as the full `Service`. `StartService`,
  `GetService` and the shared service of the chat use a list of `Hash`.
- `AttrHashTag` had no reader: only the packer wrote the hash of the filesystem, and
  the only reader looked for the architecture, which the packer never wrote. It named
  the parts by field number from outside the hashed bytes, so a change of a field
  number broke it with no error.
- The names of a service (`HashTag.tag`) are not in the contract. Nothing signed or
  hashed them, so a peer could give any name to any service. A name is a local label
  of the node and of the CLI (from `service.json`). A text for a reader is in
  `Service.prose`, which the id covers.

### Devices

- The architecture of a device is what the software of the service programs against,
  as for the architecture of the container (open question 5, REASON 2.4, 7.3). Its
  formal fixes the granularity: a family of GPUs with one compute capability, a USB
  video class camera, one model of robot arm or of FPGA. With "one model of chip", a
  CUDA service for four GPUs had to be four services, and a service for any UVC
  camera fifty. A model that the software does not need is a fact of the device.
- The guest kernel is a requirement of the service, not a property of
  the device, so it is a setting in `Container.Init.policies`. Its value gives the
  hashes of the kernel, one for each hash type. An alias of a kernel ABI is a reading
  of the node (REASON 2.5), not a new identity. With no kernel setting, the service
  has no preference and the node selects the kernel.
- The kernel is not in the bytes of `Container.architecture`. The architecture is the
  key of `Peer.resources` (capacity and benchmarks), and the kernel changes neither
  (REASON 3.5). It is a choice applied to one machine for each instance, not a
  property of the machine (REASON 2.4). The formal of an architecture can say that it
  boots a guest kernel, and which setting selects it.
- The initramfs is not a setting of the service. It is the boot mechanism of the node
  (its `/init` mounts the filesystem and starts the entry point), so it is a part of
  how the node implements the architecture.
- An architecture is never empty, in `Container` and in `Device`. An empty
  architecture does not mean the native architecture of the node.
- One `Device` is one device as the guest sees it: a physical device, or a part of
  one (a virtual function, a partition of a GPU). One entry of `Peer.devices` is one
  such device, as the node is configured now. A device that can boot more than one
  kernel is not counted two times.
- `ArchitectureResources.facts` (an announcement) gives the facts of a device: its
  model, a stable id, where it is, the devices that it is linked to. A rate of a
  device can use the model as its scope (`Rate.scope`). A node does not take the
  model from the text of a tag.
- `Device` or `Network`: the bus does not decide (PCI, USB, a bus of a board or a
  network of the node). A `Device` is a thing of the node that the node gives to one
  instance at a time, takes back and charges for: a camera on a private segment that
  the node opens only to the instance is a `Device`, and its `guest_address` is an
  address in the network of the instance. A thing that the instance reaches and
  whose access the node does not control (a camera on the internet, or on the LAN of
  the node when the node does not announce it) is a `Network`. A device that many
  instances share is a service that holds the device and gives an API (REASON 1.6).
- A situated device (a camera that looks at one place, a robot arm in one lab, an
  HSM with its keys, a disk with data) is selected by the caller, not by the service:
  the service has an id and stays generic. The caller gives, in
  `Configuration.policies`, the stable id of the device for an entry. At the wake,
  the node gives the same device again, or the instance stays hibernated and the
  answer says why.
- Before the node detaches a device by its own decision (a hibernation or a stop that
  the instance did not ask for), it gives the notice that the settings of the device
  ask for (for example, a robot arm must stop in a safe position). A node that cannot
  give that notice refuses the service.
- If a device fails or the node loses it, the node detaches it and stops its charge.
  `DeviceChange.Attach` accepts any entry that has no device now, so an entry that
  the node attaches at the start can come back without a hibernation.
- The setting of the attach mode has, as its value, the ordered list of the modes that
  the service accepts. `Peer` does not announce the modes: `GetResourceAvailability`
  gives the answer.
- The kernel and the models of the devices that a start attaches are
  `StartReceipt.facts`. The models are one formal whose value is the list (a list of `Policy` has no repeated formal).
- A node that cannot grow a device (for example `at_init` less than `at_most` under
  VFIO) refuses and names the formal. A device benchmark that the node did not
  measure does not meet a requirement: the node refuses.
- `Container.devices` is in the id of the service, so its order is fixed: the
  position of an entry is its number, and a reader does not sort the list. The list
  can repeat an entry. The order of `Peer.devices` has no meaning.
- `DeviceChange.attach` names the entry of `Container.devices` by its position, with
  the resources that the instance asks for. It is no longer an
  `ArchitectureResources`: two entries with the same architecture and other settings
  were ambiguous, and its field of benchmarks had no meaning there (REASON 3.5).
  `AttachedDevice.entry` gives the entry that a device fills.
- `DeviceChange.resize` changes the resources of a device that the instance has.
  Before, `Device.resources.at_most` had no RPC that used it. A node that cannot
  resize (VFIO) refuses and names the formal. Today MIG partitions and virtual
  functions do not change their size while they run, so `resize` is for the future
  (REASON 1.7).
- A hibernated instance has no devices. At the wake, the node attaches again the
  entries that it attaches at the start, with the resources that they had, maybe on
  other physical devices and with new ids. The new process asks again for the
  devices that it attaches on request. A `DeviceChange` is only for an instance that
  runs.
- A partial service of a workload can declare devices.
- What PR #649 (VFIO, attach at the start only) must change when the code follows:
  tags `pci:<vendor>:<device>+<alg>:<hash>` become an architecture formal (the family
  that the software needs), a model fact, and the kernel setting; the price and the
  receipt read the model fact; an unmeasured benchmark
  refuses; the receipt fields of its proto diff become `facts`. Its release on
  hibernation and its new selection on wake already follow the default of
  `Device.policies`.

## Open questions

Each question keeps its number when it closes. Closed: question 1 (see
"Dependencies"), question 2 (see "Networks"), question 3 (see "Receipts"), question 4
(see "Schemas"), question 5 (see "Devices") and question 8 (see "Hashes and
names").

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
- Kinds of network (`Network.domain`): all addresses, a host name with its ports, a
  domain of celaut instances, and a proof-of-work network for each chain (Ergo,
  Bitcoin). The current `pow.` keys and `port=` go into the values.
- Settings of a network: the `${VAR}` template, with its syntax and the check of the
  filled value (today a value is a placeholder in full or has none, and has no new
  line).
- The id of a network share, in the formal of the share setting.
- Devices: the architectures of devices (GPU families by compute capability, the USB
  video class, ...), the model fact (one formal for each PCI or USB vendor and
  device pair, made from one template text), the stable id fact, the facts of place
  and of links, the setting of the guest kernel, the selection of a device by the
  caller, the notice before a detach, the links between entries, the bitstream of an
  FPGA, the device benchmarks, the rate of a device for each model, and a promise to
  wipe a device between two clients.
- The schemas (`Service.schema`, `Signed.Body.context`): the format of a schema (today
  `src/identity/protocol_schema.py`, plus the name of each field), and one schema for
  `Service` and for each signed type. The layer `celaut-gateway` uses the same format,
  also with the names of the fields: without them, two variants that add one field
  number with the same type have the same schema.

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
