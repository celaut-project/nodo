# Pending: quantities, resources and hibernation in celaut.proto

This file records a redesign of `celaut.proto` that is not complete. It has the
decisions that the proto already shows, and the questions that are still open. The
code (Python, Rust TUI), the generated `*_pb2.py` files, the tests and `compat.json`
do not follow the proto yet. Remove this file when the last open question is closed
and the code follows the proto.

## The two levels

The protocol has two levels:

1. **The contract** (`celaut.proto`). It is static and difficult to update. It must
   not change when a new thing appears.
2. **The formal definitions** above the contract: policies, resources, benchmarks,
   rates, limits, architectures, APIs, protocols. A formal is a set of bytes that a
   document defines. Nodes can share, negotiate or ignore formals. A new thing is a
   new formal, never a new field.

The goal is a protocol that grows at the second level, with no change to the first.
For each open question below, the first question is: which level does this belong to?

## Decided (the proto shows it)

- `Quantity` (`tags`, `prose`, `formal`, `optional uint64 value`) replaces the typed
  fields of `Sysresources` and the `benchmark` key-value list.
- Two classes of formal: **resource** (consumed: reserved, added, enforced, charged)
  and **benchmark** (measured: compared one by one, never added). The class is part
  of the definition of the formal. A node refuses a formal in a list of the wrong
  class.
- One comparison rule for all formals: more is better. A latency is a rate.
- Lists of quantities: no two equal formals, sorted by formal bytes. A quantity
  without a value says nothing.
- An unknown formal in a requirement or a limit: the node refuses. An unknown formal
  in an announcement: the node keeps it and does not use it.
- `Container.Resources` has three lists: `at_init`, `at_most` (resources) and
  `benchmarks` (minimum scores for all the life of the instance). A resource that is
  not in `at_init` is 0 at the start. A resource that is not in `at_most` cannot grow
  above `at_init`. For each formal, `at_init` <= `at_most`.
- Stability of a score is a formal of a percentile (for example the 5th percentile
  over windows of one second). It is not a separate "volatility" field.
- The node compares the benchmarks each time that it places an instance: at the
  start, at a wake and at a move.
- `Sysresources` is removed. Each place names its lists:
  `ArchitectureResources.resources` and `.benchmarks`,
  `ConfigurationFile.initial_resources`, `ModifyServiceSystemResourcesInput.resources`,
  `ModifyServiceSystemResourcesOutput.resources`, `ExecutionReceipt.reserved`.
- `ModifyServiceSystemResourcesInput.min_sysreq` is removed (it had no use).
- `ResourceAvailabilityRequest` has `architecture` and a `Container.Resources`, as a
  service declares them. It has `at_init` again.
- `EstimatedCost.launch_scarcity` (parts per million) is replaced by
  `launch_reserved`: what the node had reserved, in the unit of each resource. The
  scarcity is `reserved / capacity`, an exact quotient with no scale in the contract.
- `Peer.rates` is a list of `Rate` (`formal`, `Amount value`, `scope`). `Peer.limits`
  is a list of `Limit` (`formal`, `uint64 value`, `scope`). `scope` holds the part of
  the old keys that changed (`:<architecture>`, `:<network>`). Two entries do not have
  the same formal and scope.
- `Uint64KeyValue` is removed.
- Hibernation is elemental (REASON.md, rule 1.5). An instance runs (there is a
  process) or is hibernated (there is no process). There is no third state:
  "suspended" (low balance) and "hibernated" are one state with different causes.
  The proto describes it once, in the comment "The life of an instance", before
  `message Instance`.
- A hibernated instance keeps its identity and its state (the filesystem as the
  process left it). The node reserves none of its resources. No resource is related
  to hibernation: the resources keep their values, which are what the instance gets
  when it wakes.
- `ModifyServiceSystemResourcesInput.hibernated` (optional bool) asks for the state.
  No value of a resource hibernates or wakes an instance (the old magic zero is
  removed). `ModifyServiceSystemResourcesOutput.hibernated` gives the state.
- `Metrics.suspended` becomes `Metrics.hibernated`.
- `OnClose` stays an enum: its set is closed (keep the process, lose it, end).
- Memory is not different from other resources in the contract (REASON.md, rule
  3.4): the place of the filesystem of the container is a property of the formal of
  a resource, and it depends on the node.

## Open questions

### 1. Hibernation: what is still open

The contract part is decided (see "Decided"). These parts are open:

1. **A hibernated instance that cannot pay.** The node charges a hibernated instance
   for the state that it keeps. When the balance is too low for the maintenance, the
   instance hibernates, and then nobody pays for the state. Today the node does not
   charge it and keeps it until a deposit. Decide: a period of grace and then a stop
   (a retention policy of the node, as a formal), or no charge for that case.

2. **The way to continue (a policy of the service).** Today a wake starts a new
   process from the entrypoint, on the kept filesystem. Another way is to keep an
   image of the memory, so the process continues where it was. A service that keeps
   its state only in memory cannot accept a new start from the entrypoint. This is
   a requirement of the author, so it is a `Policy` in `Init` (the first policy
   there): the ways to continue that the service accepts. A node that cannot do one
   of them does not hibernate the service: it stops it, or refuses it in the quote.
   Decide the default: the fail-closed rule (REASON.md, rule 3.2) points to "no new
   start from the entrypoint unless the service accepts it". That changes the
   behavior of all the services of today, which must then declare the policy.
   The same policy applies to every new start on a kept state: a wake, a move, or a
   recovery after a restart of the node.

3. **A wake in the execution receipts.** A wake starts a new process. The receipts
   have the kinds "start", "checkpoint" and "close". Decide if a wake has its own
   kind ("wake"), so that the receipts of an instance show each process.

4. **Network shares.** The node refuses to hibernate an instance that uses a network
   share. Find why (the node holds a share while one of its instances uses it), and
   decide if this is a rule of the contract or a rule of a hibernation policy.

5. **The rate of the kept state.** A `Rate` formal for the state of a hibernated
   instance (for example MU per GiB of kept state and second). It is not the rate of
   the disk: the node can keep the state on another, slower store. Decide how the
   node measures the size of the kept state.

6. **The `wake` policy.** `docs/policies/wake.formal` ("wake=on_tunnel") stays. Its
   prose must say "wakes a hibernated instance" with the meaning of "The life of an
   instance". Its formal bytes do not change.

### 2. The maximum scarcity multiplier

`scarcity_max_multiplier` was a key of `Peer.rates`. It is a number with no unit, not
a price in MU, so it is not a `Rate`. Options:

- A field of `Peer` (contract level). Simple, but it fixes the pricing model in the
  contract.
- A parameter of each resource rate (one maximum for each resource).
- A formal of the pricing model, with its parameters as a `Policy`-like entry. This
  keeps the pricing model at the second level.

### 3. The id of a formal in text

A formal is bytes, often with newlines. Text places need a short key for it:
`BalanceMovement.detail` (JSON), the CLI, the logs, the TUI, `config.yaml`. Proposal:
the lowercase hex of a hash of the formal bytes (the same hash for all formals). The
proto already refers to "the id of its formal" in `BalanceMovement.detail`.

### 4. The format and the folders of the formals

- Folders: `docs/resources`, `docs/benchmarks`, `docs/rates`, `docs/limits` (to
  confirm).
- Format: key=value lines, as `docs/policies/*.formal`. A resource formal gives at
  least: the class, the thing, the unit, total or per core, and how the node measures
  or enforces it. A benchmark formal also gives the statistic (mean or percentile),
  the window and the working set when it applies.
- A test pins the bytes in the code of the node to the files, as for policies.

### 5. The core formals to write

- Resources: memory, disk, CPU (one formal for the cores, in place of the CFS pair
  `cpu_period` / `cpu_quota`), block I/O weight.
- Device resources: device memory (not the main memory).
- Benchmarks: the current keys (`int_ops_per_sec`, `flt_ops_per_sec`,
  `sha256_hashes_per_sec`, `mem_bandwidth_<size>_bytes_per_sec`), each as a mean, and
  the percentiles to support.
- Rates: the kept state of a hibernated instance (see 1.5), and one for each old key
  (`ram_mu_per_gib_second`, `cpu_mu_per_vcpu_second`,
  `disk_mu_per_gib_second`, `net_mu_per_gib`, `build_mu`, `tunnel_open_mu`,
  `connection_open_mu`, `modify_resources_mu`, `statement_query_mu`,
  `statement_row_mu`).
- Limits: `max_concurrent_connections`, `max_connections_per_day`,
  `max_connections_per_month`, `max_gib_per_day`, `max_gib_per_month`, and a
  "global" formal for each (the old `global_` prefix).

The rule for the old memory bandwidth (use the score of the smallest larger working
set) must go into the benchmark formals, or be removed.

### 6. Benchmarks over time

- A percentile needs repeated measures. `src/utils/benchmark.py` measures one time
  now. Decide when and how often a node measures.
- If a score falls below the minimum of a service at a wake or a move, what does the
  node do? (Refuse the wake and keep the instance hibernated, or stop it and refund.)

### 7. Places that still name one resource

- `Container.devices`: "the memory of the instance must have the same value in
  `at_init` and `at_most`". It names the memory.

Decide if the contract can name a resource, or if each such rule must move into a
formal.

### 8. Signed and hashed messages

These change with the new fields, and their documents and tests must follow:

- the canonical encoding of the service id (`Container.Resources`),
- the digest of `Peer` (`rates`, `limits`, `resources`, `devices`),
- the signed `EstimatedCost` (`launch_reserved`),
- the signed `ExecutionReceipt` (`reserved`),
- `docs/CANONICAL_ENCODING.md`, `compat.json`.

### 9. The signature scheme (rule 1.6 of REASON.md)

`Peer.signature_scheme` is already a formal: a set of components, each one with
`tags`, `prose` and `formal`. So a node that does not speak the scheme of a peer can
look for a celaut service that verifies signatures of that scheme, run it, and
verify the peer (REASON.md, rule 1.6). The proto does not follow this yet:

1. **The comment on `Peer` says the opposite.** It says: "an unknown *signature* is
   an authentication decision with no third answer: accepting one unverified admits
   anyone". The first part is wrong now: verification through a service is the third
   answer. The second part stays true: a node never accepts a signature that nobody
   verified. Also, `SignatureScheme` says: "A node can implement only some schemes,
   and it refuses a Peer with a scheme that it does not speak". Change it: the node
   refuses only if it does not find, or does not want to use, a service that
   verifies the scheme.

2. **`public_key` fixes a size that the scheme defines (rule 4.6).** The field says
   "as 64 lowercase hexadecimal characters". A scheme with larger keys (for example
   a post-quantum scheme, or a hybrid with two keys in one descriptor, which the
   comment on `Peer` allows) breaks this. The encoding of the key belongs to the
   scheme. The contract can only say: lowercase hexadecimal of the bytes that the
   scheme defines. Then check the id of the peer (today, the key itself): if keys
   can be long, the id may need to be a hash of the key. Also check the code and the
   docs that depend on 64 characters (`peer_id` columns, `client_ref`, the chat
   evidence, the TUI).

3. **A component can have no formal (rule 2.2).** `SignatureScheme.Protocol.formal`
   is "the parameters of the component ... if they are fully known. Else empty", and
   two components without a formal pair by one common tag. Then the identity of a
   component is its tags, not exact bytes. Two problems:
   - Two nodes can pair components that only share a name, but are not the same.
   - A node has no exact key to look for a service (rule 1.6). With tags only, the
     search is a guess.

   Decide if every component must have a formal, or if a component without a formal
   is accepted only for a search, and never for a pairing.

4. **There is no mechanism to find a service by a formal.** Rule 1.6 needs it. The
   questions:
   - How does a service say which formal it implements? A natural place is its
     `Api`: a slot whose protocol has the formal of the function, for example
     "verify signatures of the scheme X".
   - How does a node find such a service? In its own registry, from its peers, or
     from a service that indexes others. Check if the contract already lets a node
     ask "which service implements this formal?", or if something is missing.
   - How does the node call it and read the answer? For a verifier, the input (key,
     message, signature) and the output (valid or not) must have one form for all
     the verifiers of all the schemes. That form is a formal (an API), not a field of
     the contract.

   This is the most important of the four items: without it, rule 1.6 is only an
   idea.

## Implementation stages (after the open questions)

1. Proto, the core formals and the class of each formal (resource or benchmark).
2. Pricing: rates, scarcity, `launch_reserved`, receipts, balance movements.
3. Enforcement: cgroups and the virtual machine from the formals.
4. Placement and balance: availability, benchmarks, devices.
5. CLI, TUI, docs, `compat.json`.
