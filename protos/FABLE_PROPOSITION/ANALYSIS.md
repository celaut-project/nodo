# celaut.proto: a reasoning exercise on the contract

For Josemi. Goal, in his words: *"Improve the celaut.proto contract by simplifying it, but
without making it vulnerable to updates (being forced to update and hard-fork the system)
in the future."*

**What was read.** `protos/REASON.md`, `protos/PENDING.md` and `protos/celaut.proto` on
`design/proto-quantities` (commit `ce57562`), the same proto on `dev`, and the nodo code on
`dev` at `f3834aa`. Every `file:line` below is in that `dev` tree.

**Files next to this one.**

- `celaut.proposed.proto`: the full proposed contract. It compiles with `protoc` 36.1.
- `proposed.diff`: the diff against the branch.
- `build/make_proposed.py`: the script that makes the proposed file from the branch file,
  one checked replacement per change.
- `RESUMEN.md`: the executive summary in Spanish.

**How sure I am.** Each claim has a tag:

- **[sure]**: I read it in the code or the proto, or I ran it.
- **[judgment]**: a design opinion. A reasonable designer could decide differently.
- **[not checked]**: I reasoned about it and did not verify it in the code.

Nothing here was pushed, commented or opened on GitHub.

---

## Executive summary

1. **The branch moves the right content out of the contract** (resource names, benchmark
   keys, rate keys, limit keys, the magic zero of hibernation). By its own count rule
   (REASON 8.6) it still grows: 90 → 91 messages, 358 → 365 fields, 2156 → 2252 lines.
2. **The largest update risk is not in the fields. It is in the signatures.** Each signed
   message (Peer, offer, statement, receipt, reputation proof) is signed over a text digest
   that lists its fields by hand. A new field cannot be covered without a new digest, and
   the digest formats are keys of the gateway layer that must be equal for two nodes to
   talk. So today **every additive change to a signed message is a hard fork**.
3. **Fix: one `Signed` envelope over the exact bytes.** It replaces six sets of envelope
   fields and more than 40 digest keys of the gateway layer with two messages. An old node then verifies and
   forwards a message with fields that it does not know. It also removes the "64
   hexadecimal characters" of the public key, which blocks post-quantum schemes.
4. **A new hash algorithm also forks the network today**, although `Hash.type` was designed
   to be open: the list of hash types is a layer key that must be equal on both sides.
5. **A new formal changes celaut.proto today.** The comments hold catalogs of formals (the
   slot policies with their exact bytes, the `pow.` keys, the share keys). `compat.json`
   hashes the file with its comments, and `nodo update` stops on any difference. The
   two-level model is correct, but the catalog is written at the wrong level.
6. **An unknown field in a Service is skipped (fail open).** That is the opposite of rule
   3.2 for a requirement. One sentence fixes it: a node does not run a service that has a
   field number that it does not know.
7. **The offer has no list of formals.** REASON 3.2 uses "what the node does when its window
   closes" as its example of an offer formal, but `EstimatedCost` holds a pricing model in
   fields, with a `float` in a signed message (against rule 6.1), and `OnClose` is an enum.
   Proposal: `EstimatedCost.terms` and `Peer.terms`.
8. **Both enums fail rule 4.4.** The comment of `Device.Attach` already plans for values
   that a reader does not know. The proposal has no enum.
9. **(tags, prose, formal) exists eight times, with four comparison rules in the code.**
   REASON 2.2 allows one. Proposal: one `Descriptor`, and a signature scheme that is one
   descriptor, not a set of components paired by permutation.
10. **The branch reuses field numbers with new types.** I ran it: a node on `dev` reads the
    disk of a new service as its memory limit, with no error. REASON has no rule about field
    numbers. It needs one.
11. **REASON.md is sound but not complete.** Rules 1.3 and 1.4 give two answers for a
    signature scheme. The elemental test cannot be decided as written. Rule 1.6 has no
    trust anchor. Rule 6.2 describes a state that the code does not have. Section 3 gives
    exact wording for each.
12. **Stress test, 15 future scenarios:** the branch absorbs 3 with formals only, and 2 in
    part. The proposal absorbs 12, and 3 in part.
13. **The proposed contract:** 91 → 65 messages, 365 → 240 fields, 2 → 0 enums, 25 → 22
    RPCs. Of these, 11 messages, 65 fields and 3 RPCs (Chat, Observe) are moved to optional
    protocols, not deleted. Each change is independent and ranked by value and risk.
14. **PENDING.md:** nine recommendations in section 4. The main ones: the default way to
    continue stays "new start from the entrypoint", the scarcity multiplier is a term of the
    node, the binding "this service implements this formal" is a reputation opinion, and a
    signature scheme has one formal.
15. **Do the renames and the type changes now.** In the layer comparison a message name is
    part of the frozen surface. After the freeze, only two kinds of change are safe: a new
    formal, and a new field in a signed announcement.

---

## 1. Audit of the proposed contract against REASON.md

### 1.1 How to read the tables

- **Level**: `C` is a mechanism that belongs to the contract. `E` is an elemental concept
  (rule 1.5). `F` is content that belongs to a formal but is in the contract.
- **Interop** (rule 1.3): must two nodes agree on it to work together?
- **Future** (rule 1.4): can a new case appear?
- A defect has a code (`A1`, `B2`, ...). Section 1.3 explains each code.

### 1.2 Element by element

**Shapes and values**

| Element | Level | Interop | Future | Verdict |
|---|---|---|---|---|
| `DataFormat` | C | yes | no | Correct shape. It is copied 7 more times under other names (C1). |
| `BytesKeyValue` | C | yes | no | Correct. |
| `AmountKeyValue` | C | yes | no | Only `Slot.mu_per_call` uses it after the branch. That field is dead (C6). |
| `DataFormatKeyValue` | C | yes | no | Correct. |
| `Policy` | C | yes | no | Correct shape. Its comment fixes "does not run the service" in the type. Rule 3.2 says that the place decides (D3). |
| `Quantity` | C | yes | no | Correct. The typed number is justified: a third party compares two values with no knowledge of the formal. |
| `Rate`, `Limit` | C | yes | no | Correct idea. `scope` is "an architecture tag or a network", which names open sets and compares by tag (A7). |
| `Amount` | C | yes | no | Correct. |
| `Contract`, `.Ledger`, `.Protocol` | C | yes | no | Correct. `Ledger` and `Protocol` are copies of `DataFormat` (C1). |
| `ContractRate` | C | yes | no | Correct. |
| `Metadata`, `HashTag`, `Hash`, `AttrHashTag` | C | yes | no | `Hash.type` is a good open mechanism. `Metadata.format` and `.reputation_proofs` say "Senders do not set it now" (C6). |

**Service specification**

| Element | Level | Interop | Future | Verdict |
|---|---|---|---|---|
| `Service` | C | yes | no | Correct. Its id is a hash of its bytes, so it has the strictest rules. |
| `Service.Api`, `.Slot` | C | yes | no | The comment of `Slot.policies` is a catalog of three formals with their bytes (A1). `mu_per_call` and `payment_contracts` are dead on a service (C6). |
| `Service.Api.Protocol` | C | yes | no | Copy (C1). |
| `Workload`, `Dependency`, `Group`, `Clause` | C | yes | maybe | Accepted. The CNF form is a mechanism. `on_filesystem` has no reader in the node (C6). |
| `Container` | C | yes | no | `node_protocol_stack` says "Senders do not set it now", which is false (D7). The comment of `devices` names the memory (PENDING 7, A5). |
| `Container.Architecture` | C | yes | no | Copy (C1). "A reader compares architectures by tag" breaks rule 2.2 (D2). |
| `Filesystem`, `ItemBranch`, `Link` | C | yes | no | Two comments are catalogs: `share`, `read_mode`, `entry_metadata` (A1). |
| `Init` | C | yes | no | Correct. The empty policy list is a good extension point. |
| `ConfigDeclaration` | – | no | – | Vestigial: the packer writes it, and no code of the node reads it (C6). |
| `NoticeDeclaration` | F | yes | yes | A requirement of the author in a shape of its own. It is a `Policy` (C5). |
| `Container.Resources` | C | yes | no | Correct. "for all its life" is a promise that rule 5.3 forbids (D5). |
| `Network` | C/F | yes | yes | Tags are data (host names), against rule 2.2. The comment is a catalog of `pow.` keys (A1). "A reader keeps a key that it does not know" is fail open in a requirement (B2). |

**Instance and configuration**

| Element | Level | Interop | Future | Verdict |
|---|---|---|---|---|
| "The life of an instance" | E | yes | no | Accepted as elemental, with the caveats of section 3.2. |
| `Instance`, `Uri_Slot` | C | yes | no | Correct. |
| `Instance.Uri` | C | yes | no | A prefix of `Peer.Uri` with no transport (C2). |
| `Configuration` | C | yes | no | The reserved key `__nodo_instance_name` is a magic value (rule 4.3). It shows that an extension point is missing (B3). |
| `ConfigurationFile`, `NetworkResolution` | C | yes | no | Correct. `gateway` carries the rates of the node in `mu_per_call`, a second form of `Peer.rates` (C4). |

**Offers, resources and devices**

| Element | Level | Interop | Future | Verdict |
|---|---|---|---|---|
| `EstimatedCost` | C + F | yes | yes | `cost`, the two maintenance costs, the period and `valid_until` are a mechanism to compare offers. The rest is a pricing model in fields (A2). `price_volatility` is a `float` in a signed message (D4). |
| `OnClose` (enum) | F | yes | yes | The three outcomes are closed by logic. The three values are not only outcomes: each one adds a behavior (A3). |
| `ActivityWindow`, `Span` | F | no | yes | One model of a schedule ("one repeating day"). Two nodes work together without it (A4). |
| `Device` | C | yes | yes | The tag grammar `pci:<vendor>:<device>+<algorithm>:<hash>` is content (A5). In `Peer.devices`, "only at_most and benchmarks have values" (rule 4.2, D6). |
| `Device.Attach` (enum) | F | yes | yes | "A reader keeps a value that it does not know": an enum that plans to grow (rule 4.4, A3). |
| `ArchitectureResources` | C | yes | no | Four meanings by place, and "Empty where the message tells of a request" (rules 4.1 and 4.2, D6). |
| `ResourceAvailabilityRequest` | C | yes | no | It is a partial `Service.Container`: architecture, resources, devices (C3). |
| `ResourceAvailability` | C | yes | no | Correct. It cannot say which formal the node did not know (B5). |
| `AttachedDevice`, `DeviceChange` | C | yes | yes | Accepted. `guest_address` is "the PCI address" (A5). |
| `ModifyServiceSystemResourcesInput/Output` | C | yes | no | The explicit `hibernated` is correct. The network-share sentence is a limit of one implementation (PENDING 1.4). |

**Balance, evidence and reputation**

| Element | Level | Interop | Future | Verdict |
|---|---|---|---|---|
| `TokenMessage` | C | yes | no | "Other methods do not use it" on two of three fields (rule 4.2, D8). |
| `DescendantTunnel` | C | yes | no | A second first message for the same RPC (C3). |
| `Refund`, `Payment`, `ModifyDepositInput` | C | yes | no | Correct. |
| `Metrics` | C | yes | no | Two meanings by RPC. `hibernated` has no meaning for a client (D8). Its field 2 changed its meaning in place (section 6.3). |
| `BalanceStatementRequest`, `BalanceMovement`, `BalanceStatement` | C | yes | no | Correct mechanism. `kind` and `detail` are open sets as text, which is acceptable. Their catalogs are in comments (A1). The hash is blake2b-256 by name (A6). |
| `ExecutionReceipt` | C + F | yes | yes | One message for three kinds and two planes: 16 of its 27 fields say "Only in ..." or "Only for ..." (rule 4.2, D8). `kernel` is "SHA-256 hex", and the name uses "the 32 bytes of the public key" (A6, rule 4.6). |
| `FaultEvidence` | C | yes | yes | Two typed lists. An offer or a chat entry cannot be evidence (B6). |
| `ReputationOpinion`, `ReputationProof` | C + F | yes | yes | Accepted. The shape copies one ledger design (R4, R5, R8, R9). `pow_nonce` fixes one anti-spam scheme. I leave both [judgment]. |
| `ReputationProofAck`, `AssociateClientOutput`, `ModifyDepositOutput` | C | yes | no | The same message three times: a bool and a reason (C3). |
| `ServiceInstance` | C | yes | no | Correct. |

**Identity, clients and the rest**

| Element | Level | Interop | Future | Verdict |
|---|---|---|---|---|
| `Peer` | C | yes | no | The envelope fields and the hand-written digest are the main update risk (B1). `public_key` is "64 lowercase hexadecimal characters" (rule 4.6, PENDING 9.2). |
| `Peer.Uri`, `.Uri.Protocol` | C | yes | no | Correct. `Protocol` is a copy (C1). |
| `Peer.SignatureScheme`, `.Protocol` | C | yes | yes | A set of components paired by permutation, with a tag fallback (rule 2.2, PENDING 9.3). One descriptor is enough (C1). |
| `Client` | C | yes | no | Accepted. The binding signature is one more hand-written text (B1). |
| `PoWRequired` | C + F | yes | yes | It has a `formal`, and `difficulty` still fixes "the number of 0 characters at the end of the hexadecimal digest" (rule 4.6, A5). |
| `RecursionGuard` | C | yes | no | Correct. `IntroducePeer` returns it with the token "REFUSED": a magic value in a message with another meaning (rules 4.1 and 4.3, D8). |
| `StartReference` | C | yes | no | Correct. |
| `ChatMessage`, `ChatEntry`, `ChatThreadRequest`, `ChatThread`, `ChatAck` | – | no | yes | Two nodes work together without chat. An application on top of the gateway (C7). |
| `ObserveRequest`, `ObserveEvent` and its 4 messages | – | no | yes | A debugging stream for the owner. It names resources and tools: `cpu_percent`, `mem_bytes`, `tcp_flags`, the libpcap LINKTYPE, "pcap", "conntrack" (rule 5.1, C7). |

**RPCs** (all are `stream Buffer → stream Buffer`, so the proto holds only their names and
comments; the types at each index are in the gateway layer)

| RPC | Verdict |
|---|---|
| `StartService`, `StopService`, `ModifyDeposit`, `GetService`, `Payable`, `GenerateDepositToken`, `GetBalanceStatement`, `LocateShare`, `ResolveNetwork` | Contract. Correct. |
| `GetPeerInfo`, `ListPeers`, `GetReputationProofs`, `PushReputationProof` | Contract. The comment of `ListPeers` holds a behavior of one node ("does not send an advertisement with a non-global address or a DNS name"). It is a policy of the node, and it would stop the gossip of onion addresses (A5). |
| `IntroducePeer` | Contract. It returns a `RecursionGuard` with a magic token (D8). |
| `GenerateClient`, `AssociateClient` | Contract. Correct. |
| `ModifyServiceSystemResources` | Contract. Correct after the branch. |
| `GetServiceEstimatedCost`, `GetResourceAvailability` | Contract. They overlap. The second exists for a workload with no service, and it can take a partial `Container` (C3). |
| `GetMetrics`, `GetClientMetrics` | Contract. One message for two meanings (D8). No RPC gives the addresses of an instance after the start (B4). |
| `ServiceTunnel` | Contract. Three different first messages at three indices (C3). |
| `Observe`, `Chat`, `GetChatThread` | Not contract (C7). |

### 1.3 The defects

#### A. Content that is frozen in the contract

**A1. Catalogs of formals in comments. [sure]** The comment of `Slot.policies` lists
`access`, `tunnel_auth` and `wake`, each with its exact formal bytes (branch lines
268–292). The same is true of `share` (391–416), `read_mode` and `entry_metadata`
(420–447), the `pow.` keys of `Network.formal` (557–567), the kinds and the JSON keys of
`BalanceMovement` (1177–1210), and the kinds, planes and coverages of `ExecutionReceipt`.
Rule 1.2 says: "A list of the things that exist is a catalog, and a catalog grows." These
comments are that catalog. The cost is real, because of two mechanisms:

- `compat.json` holds the SHA-256 of the file `protos/celaut.proto`, comments included
  (`src/utils/compat.py:57-60`). `nodo update` stops when any entry differs
  (`src/commands/update.py:117-119`).
- So a fourth slot policy, which is a pure second-level change, edits `celaut.proto` and
  makes `nodo update` say "not compatible".

The formal files already exist (`docs/policies/*.formal`), and a test pins the bytes. The
contract only needs to say where a list is and which position it has.

**A2. A pricing model in `EstimatedCost`. [sure for the facts, judgment for the remedy]**
`price_bounded`, `price_volatility`, `volatility_period_seconds` and `launch_reserved`
describe one model: a scarcity multiplier, and a bound that grows by a compound rate for
each period. Rule 1.4 names "a price" as one case of an open set. The model arrived in
November (#636), with three new fields. The next promise (a fixed price, a cap in MU, a
price tied to an oracle) needs fields again. `launch_reserved` is state of the node: its
own comment says "A caller does not need it".

**A3. Two enums for open sets. [sure]**

- `OnClose`. The branch says that the set is closed: keep the process, lose it, end. That
  is true for the *outcome*. But `ON_CLOSE_REFUSE` also means "refuses a new child and a
  resource increase until the window opens", and `ON_CLOSE_HIBERNATE` also means "wakes it
  when the window opens". These are behaviors, and behaviors are an open set. The comment
  says that a move to another node "is a hibernation here and a wake there". For the
  caller, "asleep until 08:00" and "running on another node at another price" are not the
  same term of an offer. REASON 3.2 itself gives "what the node does when its window
  closes" as its example of a formal in an offer. So the same concept is an enum in the
  proto and a formal in the rules (rule 7.3).
- `Device.Attach`. Its field says: "A reader keeps a value that it does not know, and
  does not use it." An enum whose text plans for unknown values is an open set by the
  definition of rule 4.4. It also has an order with a meaning ("the first one in the list
  that it supports"), against rule 3.1.

**A4. `ActivityWindow` is one model of a schedule. [judgment]** "One repeating day" in
UTC minutes cannot say a weekly schedule, a holiday, or "closed when electricity is
expensive". Two nodes do not need to agree on it: a caller that ignores it calls the node
and gets `can_execute = false` with `opens_at`. By rule 1.3 it is a formal.

**A5. Names of members of open sets in the text (rule 5.1). [sure]**

- "the memory of the instance must have the same value in at_init and at_most"
  (`Container.devices`, PENDING 7).
- The device tag grammar `pci:<vendor>:<device>+<algorithm>:<hash>`, and "The PCI address
  of the device in the guest" (`AttachedDevice.guest_address`). A device that is not PCI
  changes the contract.
- "The node gives one device to one instance only" (`Device`). A shared or sliced device
  changes the contract.
- `PoWRequired.difficulty`: "the number of '0' characters at the end of the hexadecimal
  digest". The message has a `formal` for the rule of the work, so this is rule 4.6.
- `Rate.scope` and `Limit.scope`: "an architecture tag or a network (a DNS host name in
  lowercase, or '*')".
- The comment of `Peer.limits`: "A day and a month are the calendar days and months in
  UTC", "concurrent connections, connections per day and per month, GiB per day".

**A6. Fixed hash and key sizes outside `Hash.type`. [sure]** Service ids are agile:
`Hash.type` is the digest of the empty input. These places are not: `instance_ref`,
`account_ref`, `prev_hash`, `head_hash`, `execution_name`, `envs_digest` (all "blake2b-256
hex"), `ExecutionReceipt.kernel` ("SHA-256 hex"), and "the 32 bytes of the public key" in
`execution_name`.

**A7. `scope` compares by tag. [sure]** An architecture has a formal, but the scope of a
rate names it by a tag. Rule 2.2 says that tags never identify.

#### B. Mechanisms that are missing

**B1. A signature that covers what the reader does not know. [sure]** This is the most
important finding of the exercise.

- The digest of a `Peer` is built field by field
  (`src/identity/node_identity.py:614-703`). The docstring says why: protobuf
  serialization is not canonical. The price is stated in
  `tests/test_peer_content_digest_covers_every_field.py:1-19`: "Adding a field to
  `celaut.proto` fails these until someone decides which it is."
- The same design is used for the offer (`src/utils/cost_functions/signed_offer.py:54-74`),
  the statement, the receipt, the reputation proof and the chat entry.
- Each digest format is a key of the `formal` of the `celaut-gateway` layer
  (`src/identity/transport_stack.py:445-467` for the Peer, `499-509` for the offer,
  `512-518` for the receipt).
- Two layers are compatible only if every key that is not `schema.*` or `rpc.*` is on both
  sides with the same value (`transport_stack.py:935-961`). A node does not use an
  address whose stack is not compatible (`transport_stack.py:862-873`, called at
  `src/manager/manager.py:424`).

Together: a new field in a signed message is either not covered by the signature (a relay
can change it), or it changes a digest key, and then old and new nodes stop talking. The
schema comparison was written to allow additive fields (`transport_stack.py:923-932`),
but the digests cancel that for every signed message. The branch itself changes
`peer.digest.sysresources`, `peer.digest.rates`, `peer.digest.limits`, `offer.digest` and
`receipt.digest`.

**B2. An unknown field in a requirement is ignored. [sure]**
`docs/CANONICAL_ENCODING.md` ("Fields that the reader does not know") says that a node
with an older schema skips a field that it does not know. For a `Service` that is fail
open: an old node runs a service and ignores a requirement that a newer author wrote. The
`Policy` lists exist to avoid this, and their text says so ("so that a setting defined
later is safe on nodes that are old"). But the places with no list are not safe, and
`Network.formal` says "A reader keeps a key that it does not know", which is the same
problem inside a formal.

**B3. Places with no extension point where a real case exists (rule 7.2). [sure for the
cases]**

- `EstimatedCost`: no list for the terms of an offer. The comment of `Limit` says "the
  quote (EstimatedCost) is the authority for one instance", but the quote has no field
  that can hold a limit.
- `Peer`: no list for what a node declares about its behavior. PENDING 2 (the scarcity
  multiplier) and PENDING 1.1 (how long the node keeps a hibernated instance) both look
  for this place.
- `Configuration`: no list for an option of the caller. The reserved environment key
  `__nodo_instance_name` is the workaround.
- `Network` and `Device`: no list for a requirement that is added later.

**B4. No way to read the addresses of an instance after its start. [sure]** Only
`StartService` returns an `Instance`. The branch defines a move as "a hibernation here and
a wake there", and also says that a hibernated instance keeps "its addresses and ports".
Both cannot be true for a move to another node. A caller has no RPC to learn the new
addresses.

**B5. A refusal cannot name the formal that caused it. [sure]** Rules 1.6 and 3.2 need
the caller to know *which* formal the node did not know, to try another node or to look
for a service. `ResourceAvailability.reason` is "For a person: do not parse it."

**B6. Evidence is limited to two types. [sure]** `FaultEvidence` has `statements` and
`receipts`. A signed offer that the node did not keep, or a signed chat entry
(`docs/CHAT_EVIDENCE.md`), has no place.

**B7. No mechanism to find a service by a formal (PENDING 9.4). [sure]** See section 4.

#### C. Duplicated concepts and redundant fields

**C1. Eight copies of (tags, prose, formal). [sure]** `DataFormat`, `Contract.Ledger`,
`Contract.Protocol`, `Service.Api.Protocol`, `Container.Architecture`, `Peer.Uri.Protocol`,
`Peer.SignatureScheme.Protocol`, and the first three fields of `PoWRequired` and of
`Network`. The code compares them with four rules:

| Rule | Where | Used for |
|---|---|---|
| Exact bytes of the formal | policies | `Policy` |
| Formal if both have one, else one common tag | `node_identity.py:360-363` | signature components, networks |
| Both formals not empty and equal | `node_identity.py:381-382` | ledgers, payment protocols |
| Key by key, with additive keys allowed | `transport_stack.py:833-844` | layers of an address |
| By tag only | the packer writes only tags, `src/packers/zip_with_dockerfile.py:893` | architectures |

REASON 2.2 allows only the first.

The signature scheme is the most complex case. It is a set of components, compared by a
search over all permutations with a cap of 5 components (`node_identity.py:250-253`,
`429-442`). The only scheme that exists has one component, and its formal already holds
every parameter: algorithm, curve, hash, sizes, encodings (`node_identity.py:212-239`).

**C2. `Instance.Uri` and `Peer.Uri`. [sure]** The first is the first two fields of the
second.

**C3. Messages that are the same message. [sure]**

- `AssociateClientOutput`, `ReputationProofAck`, `ModifyDepositOutput`: a bool and a text.
- `ResourceAvailabilityRequest` is a partial `Service.Container`. The idea of a partial
  service is already in `Workload.Dependency.service`.
- `TokenMessage` (with `slot` and `receipts`) and `DescendantTunnel` are two forms of one
  request.
- `Metrics` and `ModifyServiceSystemResourcesOutput` both give the state of an instance.

**C4. The rates of the node in two forms. [sure]** `ConfigurationFile.gateway` gets
`mu_per_call` from `peer.rates`, and `payment_contracts` from the Peer
(`src/gateway/utils.py:529-530`). After the branch, `Peer.rates` is a list of `Rate` with a
formal and a scope, and `mu_per_call` is still a list keyed by a text name. One concept
has two forms (rule 7.3).

**C5. `NoticeDeclaration` is a policy. [judgment]** "A node that does not know the system,
the format or the path refuses to run the service" is the exact rule of a `Policy`. Rule
8.3 says to use a shape that exists.

**C6. Fields with no use (rule 4.5). [sure]**

| Field | Evidence |
|---|---|
| `Metadata.format` | No code sets it or reads it. |
| `Metadata.reputation_proofs` | Only `src/commands/inspect_service.py:44` reads it. No code sets it. An opinion about a service already has a form: `ReputationOpinion`. |
| `Service.Api.payment_contracts` on a service | The comment says that senders do not set it. |
| `Slot.mu_per_call` | "Only for information: the node does not charge it." Only the packer writes it (`zip_with_dockerfile.py:961`). |
| `Container.config_declaration` | The packer writes it (`zip_with_dockerfile.py:887-890`). No code of the node reads it. The comment says that the node can ignore the path. |
| `Dependency.on_filesystem` | Only the packer validates it (`src/packers/service_json.py:123-146`). |

**C7. Chat and Observe. [judgment]** They are 11 messages, 65 fields and 3 RPCs. Two
nodes work together without them. The mechanism for an optional protocol already exists:
an address announces its own stack, and a reader "does not call an address that speaks
other protocols".

#### D. Places where the branch breaks its own rules

| Code | Rule | Where | What |
|---|---|---|---|
| D1 | 8.6 | whole file | The count goes up: +1 message, +7 fields, +96 lines. The change passes only by the second clause (it moves content to formals). |
| D2 | 2.2 | `Architecture`, `Network`, `SignatureScheme.Protocol`, `Device.architecture`, `scope` | Identity by tag. |
| D3 | 3.2 | `Policy`, `Rate`, `Limit` | The comment of the type fixes what a reader does with an unknown formal. `ConfigurationFile.initial_resources`, `ExecutionReceipt.reserved`, `ModifyServiceSystemResourcesOutput.resources` and `EstimatedCost.launch_reserved` do not state a position, though "every list states which of the two positions it is in". |
| D4 | 6.1 | `EstimatedCost.price_volatility` | A `float` in a signed message. The digest signs its 4 bytes (`signed_offer.py:70`). The bound is calculated from the Python `repr` of the float (`src/utils/cost_functions/price_bound.py:69`). An implementation in another language must copy that detail to check a `maintenance_overcharged` fault. |
| D5 | 5.3 | `Resources.benchmarks` | "The minimum scores that the instance needs, for all its life." PENDING and rule 5.3 both say that a node cannot promise that. |
| D6 | 4.1, 4.2 | `ArchitectureResources`, `Peer.devices`, `DeviceChange` | "In Peer.resources ... In AttachedDevice ... In DeviceChange ...", "Empty where the message tells of a request", "only at_most and benchmarks have values", "It has no benchmarks". This is the defect of the old `Sysresources` in a new message. |
| D7 | 5.3 (honest text) | `Container.node_protocol_stack` | "Senders do not set it now." The packer sets it when `uses_gateway` is true (`zip_with_dockerfile.py:646-648`). See section 6.5 for what this does to service ids. |
| D8 | 4.1, 4.2, 4.3 | `TokenMessage`, `Metrics`, `ExecutionReceipt`, `IntroducePeer` | "Other methods do not use it", "False ... for a client_id", "Only in start", the token "REFUSED" (`src/manager/gossip.py:129`). |
| D9 | 6.2 | all signed messages | "Everything signed is canonical" and follows `docs/CANONICAL_ENCODING.md`. The signed messages do not use that encoding, and they have `int64` and `float` fields, which rule 9 of that document forbids. |
| D10 | 3.1 | `Device.attach`, `Peer.devices`, `Container.devices` | A list whose order has a meaning, and lists with repeated entries. |

---

## 2. Stress test against the future

"Formals only" means: no change to `celaut.proto`, and no change to a layer key that must
be equal on both sides.

| # | Scenario | Branch | Proposal | Why |
|---|---|---|---|---|
| 1 | A device that is not PCI, or a device that is shared between instances | No | Yes | The branch fixes the PCI tag grammar, "the PCI address", one device for one instance, and an enum for the way to attach. The proposal moves these to the formal of the device architecture and to `Device.policies`. |
| 2 | A trusted platform (TPM, confidential virtual machine) | No | Yes | The author can require it with an `Init` policy in both. The attestation that the node returns has no place in the branch: the start receipt has fixed fields (`kernel`, `devices`). The proposal has `ExecutionReceipt.facts`. |
| 3 | A new chain or payment rail | Yes | Yes | `Contract` (ledger, protocol, xattrs) and `ContractRate` are already formals. `docs/payment_protocols/` shows it for Bitcoin and Ergo. |
| 4 | Post-quantum signatures | No | Yes | The branch has "64 lowercase hexadecimal characters", "the 32 bytes of the public key", and digests in the layer keys. The proposal has `bytes signer`, `bytes signature` and one descriptor for the scheme. A scheme with large keys can define its public key as a digest of the full key and carry the full key in the signature, so the peer id stays short and the contract needs no hash of its own. The move of each identity to a new key is still a real operation: see section 6.6. |
| 5 | A new hash algorithm | No | In part | Service ids are agile in the proto. But the key `service_id.hash_types` must be equal on both sides (`transport_stack.py:440` with `935-961`), so a new entry in `HASH_SPECS` (`src/utils/hashing.py:69`) splits the network. The proposal removes the digests. The references of the ledger (`instance_ref`, `account_ref`, the chain of movements) still name blake2b-256. |
| 6 | A new architecture or virtualizer (RISC-V, WASM) | Yes, with a caveat | Yes | An architecture is a descriptor. The caveat: it is compared by tag today, so two meanings of one tag collide. It needs formals. |
| 7 | A new pricing model (a fixed price, a cap, a price for each request) | No | In part | The branch needs new fields. The proposal takes a new term for any promise or rule. The four amounts of the offer still assume a charge for each period. I keep them as the mechanism to compare offers [judgment]. |
| 8 | Onion routing and hidden nodes | In part | Yes | `Peer.Uri` already describes its transport, and the tag `tor-onion-v3` exists (`transport_stack.py:114`). `Instance.Uri` has no transport, so an instance cannot say that it is reached through onion. The comment of `ListPeers` forbids the gossip of names. The proposal has one `Uri` and removes that sentence. |
| 9 | A new way to stop and continue (a memory image, a live move) | In part | Yes | Both use an `Init` policy for the way to continue. The branch has no way to give the new addresses after a move, and `OnClose` cannot say "moved". The proposal has `InstanceState.instance` and offer terms. |
| 10 | Service composition and deeper recursion | Yes | Yes | The CNF of workloads and `remaining_hops` are mechanisms. A fee for each hop is a `Rate` formal. |
| 11 | A new reputation system, or a new kind of evidence | No | In part | Pointers to ledger proofs are formals in both. The branch cannot carry a signed offer or a chat entry as evidence. The proposal carries any `Signed`. The shape of an opinion still follows one model. |
| 12 | A bandwidth market, with limits for one instance | No | Yes | The branch says that the quote is the authority for a limit, and the quote has no place for one. The proposal puts it in `EstimatedCost.terms`. |
| 13 | Regulatory data (jurisdiction, data residency) | No | Yes | An author can require it with a policy in both. The node has no place to declare it in the branch, and the caller has no place to require it for one start. The proposal has `Peer.terms` and `Configuration.policies`. |
| 14 | A weekly schedule, or a schedule that follows the price of energy | No | Yes | `ActivityWindow` is one repeating day. The proposal makes the schedule a term of the node. |
| 15 | A market for kept state (how long a node keeps a hibernated instance) | No | Yes | PENDING 1.1 has no place for this policy of the node. The proposal: a term in the offer. |

**Result.** Branch: 3 yes, 2 in part, 10 no. Proposal: 12 yes, 3 in part.

**What the test does not show.** A formal can absorb a scenario only if nodes implement
it. "Formals only" means that no hard fork is forced. It does not mean that no work is
needed.

---

## 3. REASON.md itself

REASON.md is a good document. Its two levels, the position rule (3.2) and the role rule
(3.5) are correct, and I would keep them. The problems below are gaps and conflicts. For
each one I give wording that can go into the file.

### 3.1 Rules 1.3 and 1.4 give two answers for the same thing

A signature scheme, a hash algorithm, a transport and a payment protocol pass the
interoperability test (two nodes cannot work together if they disagree) *and* the future
test (a new case can appear). Rule 1.3 says "contract". Rule 1.4 says "formal". The
document solves it in practice with a descriptor, but no rule says so.

There are three classes, not two:

1. A constant of the contract.
2. A formal on which two nodes **must** agree to do one thing together (a scheme, a
   transport, a payment protocol).
3. A formal that a node can ignore (a benchmark that it did not measure).

> **Rule 1.4 (add).** If a thing passes the interoperability test and the future test, the
> contract holds a place for its formal, and never the thing. Two nodes then agree by the
> exact bytes of that formal. They work together for that thing only when the bytes are
> equal, or when one of them finds an implementation (rule 1.6).

### 3.2 The elemental test cannot be decided

"No other state of the same kind can appear" cannot be proved. Rule 4.4 has a test that
can be applied: "complete by logic". Use the same test.

> **Rule 1.5 (replace the test).** A concept is elemental only if you can write it as one
> statement about the basic objects of the contract, and its negation, with no third case:
> "the instance has a process" and "the instance has no process". A value of the contract
> for an elemental concept says only that statement. If the value also says what the node
> does, when, or at what cost, that part is a formal.

With this test, hibernation passes. `OnClose` passes as three outcomes, and its values
fail, because each value adds a behavior (section 1.3, A3).

One more caveat about "two states, and no other". The code has two states with no process
today: suspended (`src/database/sql_connection.py:1596`) and hibernated
(`sql_connection.py:1725`). The branch is right that the difference (who pays, what wakes
it) is a formal. But a caller must then be able to read those formals. In the branch it
cannot: `Metrics.hibernated` is one bool. The cause is only in the JSON of a movement.

### 3.3 Rule 3.2: define the position by who is bound

"Requirement" and "announcement" are clear for a `Service` and a `Peer`. They are not clear
for an answer (`ModifyServiceSystemResourcesOutput.resources`), a report
(`ExecutionReceipt.reserved`) or an offer. An offer is both: it informs, and when the
caller accepts it, it binds the caller.

> **Rule 3.2 (replace the first two items).**
> - An entry is in a **requirement** for a reader when the entry binds that reader: the
>   reader must do what it says, or the reader accepts what it says. A reader refuses an
>   entry that it does not know.
> - An entry is in an **announcement** for a reader when the entry only informs that
>   reader. A reader keeps an entry that it does not know, and does not use it. A report
>   and an answer are announcements.
> - The contract states the position of each list for each reader. No type states it.

The last sentence is necessary because the comments of `Policy`, `Rate` and `Limit` fix
the behavior in the type today (D3).

### 3.4 Rule 3.2 must also cover field numbers

The rule covers formals. Protobuf has its own unknown thing: a field number. Today it is
skipped in every place (B2).

> **Rule 3.6 (new).** A field number that a reader does not know follows rule 3.2. In a
> requirement, the reader refuses: a node does not run a service that has a field that it
> does not know. In an announcement, the reader keeps the field, and the signature covers
> it. A node that only stores or forwards a message keeps all its bytes.

This is a safety net. If a field is ever added to `Service`, an old node refuses the
service and does not run it wrongly.

### 3.5 Rule 1.6 has no trust anchor

Rule 1.6 says: "The trust moves to the service that the node runs. The node selects that
service by its content hash ... The contract does not add a trust rule for this." The
question that is left is: *who says that this hash implements this formal?* If the peer
whose signature is checked says it, the peer can name a verifier that always answers
"valid". Then the old comment on `Peer` is right again: "accepting one unverified admits
anyone."

> **Rule 1.6 (add).** A node uses a service for a formal that it does not know only when a
> party that the node already trusts binds the exact bytes of the formal to the content
> hash of the service: its operator, or publishers above its reputation threshold. A
> binding from a party that gains from the answer is not valid: the peer whose signature
> is verified, or the author of the service that is run.

Also say what rule 1.6 does not cover. A service can verify, convert or measure, because
these are functions. A service cannot enforce a policy inside the hypervisor of the node.
The rule already hints at this ("verify, convert, measure, transport"). Make it a limit.

### 3.6 Rule 2.2 against the code

Section 1.3 (C1) lists four comparison rules. Two need a decision:

- **The tag fallback.** Decide that a descriptor with no formal is valid for a search, and
  never for a decision. This also answers PENDING 9.3.
- **The layer of an address.** Its formal is not compared by exact bytes. It is compared
  key by key (`transport_stack.py:935-961`), and it is not a published file: the node
  builds it from its code at run time (`transport_stack.py:46-50`). So the most important
  formal of the network changes with each release, against "a published formal never
  changes". This is correct for a wire schema, which grows by addition. But then REASON
  must say that a second kind of formal exists.

> **Rule 2.5 (new).** One kind of formal is not compared by exact bytes: the description
> of a wire schema (the layers of an address). It is compared item by item. Two
> descriptions are compatible when they agree on each item that both have. Only items
> that can grow by addition (a field number, an RPC) go into such a formal. A rule that
> must be equal on both sides is a formal of its own, compared by exact bytes.

The last sentence is the important one. Today the digest formats, the hash list and the
proof-of-work rule are keys of the same formal as the schema, so one changed key blocks
the whole layer.

### 3.7 Rule 3.1 says "every list", and that is not true

Lists where the order has a meaning: `protocol_stack` (bottom to top), `entry_path`,
`BalanceStatement.movements`. Lists with repeated entries: `Peer.devices` and
`Container.devices` ("The node repeats an entry for each device"). Lists with a key of two
parts: `Rate` and `Limit` (formal and scope).

> **Rule 3.1 (replace the first sentence).** Every list of entries that a formal
> identifies (`Policy`, `Quantity`, `Rate`, `Limit`) has the same rules. Any other list
> states its own rules: if its order has a meaning, and if it can repeat an entry.

Also decide who enforces the order. "The sender sorts" is not enough for a message with an
id. If a reader accepts a list that is not sorted, one service has two ids.
`docs/CANONICAL_ENCODING.md` does not have this rule.

> **Rule 3.1 (add).** In a message that has an id or a signature, a reader refuses a list
> that is not sorted, or that has a repeated formal.

### 3.8 Rules 3.3 and 2.3: a requirement that is not "at least"

"More is better" and one integer cover capacity and speed. They do not cover a
requirement of an exact value (a page size, a CPU feature), or a thing that does not add
(the block I/O weight, which is relative: PENDING 5 lists it as a resource).

> **Rule 3.3 (add).** A `Quantity` is only for a thing that a node compares with "greater
> than or equal". A resource formal is a thing that adds: the sum of the values of the
> instances stays within the capacity. Any other requirement (an exact value, a relative
> weight, a feature that is present or not) is a `Policy`.

### 3.9 Rules 4.5 and 7.2 together make a trap

Rule 7.2: do not add an extension point "in case". Rule 4.5: delete what has no use, and
"after the contract is frozen, a change is a new formal, never a new field". Together: a
place that has no list at the freeze can never get one. `Init.policies` is the one place
where the branch saw this ("The list exists now so that a setting defined later is safe").

> **Rule 7.4 (new).** Before the freeze, check each message that holds a requirement. It
> must have a list of `Policy`, or it must be a message that the node refuses when it has
> an unknown field (rule 3.6). After the freeze, a new field is permitted only in an
> announcement that is signed over its bytes. A new field is never permitted in a
> requirement.

This also relaxes "never a new field". That sentence is stronger than necessary. A new
field in a signed announcement is harmless when the signature covers the bytes.

### 3.10 Rule 5.1 uses "probably"

> **Rule 5.1 (replace).** A comment and a field name of the contract do not name a member
> of an open set. A comment can give a member only after the words "for example", and the
> sentence must stay true when the example is removed.

Field names are in the rule because the proto has them: `kernel`, `pow_nonce`,
`cpu_percent`, `guest_address` ("PCI").

### 3.11 Rule 6.2 says something that is not true today

See D9. Either the rule changes, or the code does. I recommend a simpler rule, which
removes the need for a canonical encoding of signed messages:

> **Rule 6.2 (replace).** A signature covers bytes, not a message. A reader verifies the
> bytes as they came. A reader never serializes a message again to verify it. Only a
> message that has an id (a `Service`, a `Filesystem`) needs a canonical encoding.

> **Rule 6.1 (add).** A signed or hashed message has no `float`, `double` or signed
> integer field. A time is an unsigned integer.

### 3.12 Rule 6.3: the id of a formal needs a named hash

"One fixed id of its bytes" fixes one hash algorithm for ever, and
`BalanceMovement.detail` is signed evidence that someone reads years later.

> **Rule 6.3 (add).** The id of a formal in text is `<type>:<digest>`, both in lowercase
> hexadecimal. The type is the digest of the empty input, as in `Metadata.HashTag.Hash`.
> A short form is only for a display.

### 3.13 Rules that are missing

> **Rule 4.7 (new). A field number has one type and one meaning for ever.** A field that
> changes its type or its meaning gets a new number. The old number and the old name go
> into a `reserved` line. This is true in alpha too: a stored service and a stored Peer
> are older than the node that reads them.

> **Rule 4.8 (new). One message, one meaning.** Two RPCs do not share a message if a field
> has a meaning in only one of them. A refusal is a status code or an explicit field,
> never a special value of a field with another meaning.

> **Rule 3.7 (new). A refusal names its cause.** When a node refuses because it does not
> know a formal, the answer gives the bytes of that formal. Without this, the caller
> cannot use rule 1.6.

> **Rule 2.6 (new). A formal that replaces another names it.** The new formal names the
> old one by its id, and says that a list with both is not valid.

Rule 2.6 is needed because the policy formals hold their values
(`values=any,direct,tunnel` in `docs/policies/access.formal`). A fourth value is a new
formal, and then one slot can have the old `access` and the new `access`.

### 3.14 Step 6 of the process ("Count")

Count messages, fields, enum values **and layer keys that must be equal**. The gateway
layer has 89 such keys (`transport_stack.py:423-585`), and 43 of them are the formats
of a signature or of a signed digest. They are part of the
contract in practice, and no count sees them.

---

## 4. The questions of PENDING.md

### 1. Hibernation

**1.1 A hibernated instance that cannot pay. Recommendation: a grace period, then a stop.
It is a term of the offer.** The caller is bound by it (it loses its state), so by rule
3.2 it must be in the offer that the caller accepts, signed. The value is the grace period
in seconds. With no such term, the caller assumes zero: the node can stop the instance
when the balance cannot pay. No debt during the grace period. [judgment]

**1.2 The way to continue. Recommendation: a `Policy` in `Init`, as PENDING says. The
default stays "a new start from the entrypoint".** I do not agree that fail closed points
to the other default. Rule 3.2 is about a formal that the node does not know. It is not
about what an absent entry means. An author who needs more declares it, and a node that
does not know that policy refuses the service. That is already fail closed, with no change
for the services of today. One more argument: PENDING says that the same policy applies to
"a recovery after a restart of the node". A node can restart at any time. So a service
that cannot accept a new start from its entrypoint cannot run with safety on any node
today. The honest default is the behavior that every node can give. [sure about the logic,
judgment about the choice]

**1.3 A wake in the receipts. Recommendation: no new kind.** By rule 1.5 a wake is the
start of a process. So it has a "start" receipt: one for each process, in sequence. The
first one has the `initial_mu`. A wake that no caller asked for (a tunnel, a deposit, a
term of the node) has nobody to give the receipt to. Record its digest in the detail of
the wake movement. [judgment]

**1.4 Network shares. Recommendation: not a rule of the contract.** PENDING says that the
node refuses an instance that uses a network share. The code refuses more: an instance
that exports a share, or that mounts a network share
(`src/manager/hibernation.py:52-78`). The reason in the code is a limit of the
implementation: "its share lives with the VM that exports it". Another node can keep the
share alive. So the contract says only: "A node that cannot hibernate the instance
refuses, and gives the reason." The proposal has this sentence. [sure]

**1.5 The rate of the kept state. Recommendation: a `Rate` formal, charged on the value of
the disk resource of the instance. Do not measure the used size.** Rule 5.3: promise only
what can be checked. A client cannot check a size that the node measures. It can check
`rate × disk value × seconds`, because the disk value is in its signed start receipt. A
node that stores the state on a slower store announces a lower rate. [judgment]

**1.6 The `wake` policy.** Agreed. See rule 2.6 above for a later value.

### 2. The maximum scarcity multiplier

**Recommendation: the third option.** It is a parameter of a pricing model, so it is the
value of a term of the node: `Peer.terms` (what the node does now) and `EstimatedCost.terms`
(what the node keeps for one instance). It is not a field of `Peer`, which fixes the model
in the contract. It is not a parameter of each rate, because the multiplier belongs to the
model and not to the price of one thing. The same term carries what `launch_reserved`
carries today. [judgment, with confidence]

### 3. The id of a formal in text

**Recommendation: the proposal of PENDING, with the hash named.** See rule 6.3 above:
`<type>:<digest>`. In the CLI and in `config.yaml`, a person can write a tag. The node
resolves it to bytes with its own table, and stores the id. Text that is signed or hashed
has only the id. [judgment]

### 4. The format and the folders

Agreed, with four additions:

1. One root, `docs/formals/<kind>/`, and one generated index with the id of each file.
2. Each formal has the line `class=` (resource, benchmark, rate, limit, policy, term), so
   that "a node refuses a formal in a list of the wrong class" can be checked from the
   bytes.
3. State the exact bytes: the bytes of the file, with no newline at the end.
4. A formal has no path and no name of this implementation (the last sign of section 9
   of REASON).

### 5. The core formals

Agreed, with three changes:

- **Block I/O weight is not a resource.** It is relative (10 to 1000), it does not add,
  and it is not charged. By the definition of the two classes it is a `Policy`.
- **CPU:** one formal, in thousandths of a core, total. The contract has only integers.
- **Memory bandwidth:** one formal for each working set. **Remove** the rule "use the
  score of the smallest larger working set". With identity by exact bytes, a node that did
  not measure a working set does not know the answer for it. It refuses, or it measures.

### 6. Benchmarks over time

- Measure at the start of the node and then at a period that the operator sets. Announce a
  score only when there are enough measures for its percentile. An absent score is the
  honest answer.
- At a wake or a move below the minimum: keep the instance hibernated and say why. Do not
  stop it. A stop destroys the state, and that decision belongs to the owner. The comment
  "The life of an instance" already says this.
- Fix the sentence "for all its life" (D5). The proposal has the new text.

### 7. Places that still name one resource

**The contract cannot name a resource.** The rule "memory equal in at_init and at_most" is
a fact about one device on one hypervisor. It goes into the formal of the device
architecture. The general case needs no rule: a node that cannot change a resource of a
running instance refuses the request.

### 8. Signed and hashed messages

With the `Signed` envelope, four of the five items need no document: there is no digest to
write. Only the canonical encoding of the service id stays. Without the envelope, the list
of PENDING is right, and each item is a break between old and new nodes (B1).

### 9. The signature scheme

**9.1 The comment on `Peer`.** Change it as PENDING says, and keep the trust rule of
section 3.5. "A service verified it" is a third answer only if the node trusts the binding
between the scheme and the service.

**9.2 The public key.** `bytes`, in the form that the scheme defines. The id of a peer is
the lowercase hexadecimal of those bytes. I recommend against a hash of the key that the
contract defines: it puts a hash choice into the contract. A scheme with large keys can
define its public key as a digest of the full key.

**9.3 A component with no formal.** Go further than the question: **a scheme is one
descriptor with one formal.** The stack of components has no use today (C1), and it costs
a permutation search and a tag fallback. A hybrid scheme is one formal that names its two
algorithms. The detached comment on `Peer` already says "ONE scheme with one descriptor".

**9.4 Finding a service by a formal.** Three parts:

1. *A service says what it implements* in its `Api`: the top layer of a slot is a
   descriptor whose formal is the function. No change to the contract.
2. *A node finds it* through a binding (formal id → service hash). I recommend that the
   binding is a `ReputationOpinion` about the service, with the id of the formal in its
   content. It then travels with `GetReputationProofs`, and it brings the trust anchor of
   section 3.5: the node uses a binding only from publishers that it trusts. No new RPC is
   necessary. [judgment]
3. *The node calls it* with one API for all verifiers: (signer bytes, signed bytes,
   signature bytes) → valid or not. With the `Signed` envelope, those three inputs are
   already the same for every signed message.

**Leave open:** who vouches for the first verifier. Start with a list of the operator.

---

## 5. The proposed minimal contract

The full file is `celaut.proposed.proto`. It was made from the branch file by
`build/make_proposed.py`, and it compiles.

### 5.1 The count (rule 8.6)

| | dev | branch | proposed |
|---|---|---|---|
| Messages | 90 | 91 | 65 |
| Fields | 358 | 365 | 240 |
| Enums (values) | 2 (5) | 2 (5) | 0 |
| RPCs | 25 | 25 | 22 |
| Lines | 2156 | 2252 | 1611 |

Chat and Observe are 11 of the messages, 65 of the fields and all 3 of the RPCs. They are
moved, not deleted. Without them, the contract loses 15 messages and 60 fields.

### 5.2 The changes, ranked

Value: how much it reduces a future forced update, or how much it simplifies. Risk: how
much code changes, and how likely a mistake is. Each change is independent, except where
the table says so.

| # | Change | Value | Risk | Sure? |
|---|---|---|---|---|
| P1 | `Signed` envelope: a signature over exact bytes | Very high | High | [sure] about the problem, [judgment] about the shape |
| P2 | `terms` on the offer and on the Peer; remove the price fields, `OnClose`, `ActivityWindow` | High | Medium | [judgment] |
| P3 | Remove the catalogs of formals from the comments | High | Very low | [sure] |
| P4 | One `Descriptor`; a signature scheme is one descriptor | Medium | Low | [sure] |
| P5 | Remove the dead fields | Medium | Low | [sure] |
| P6 | Policy lists on `Network`, `Configuration`, `Device`; `facts` on the start receipt | Medium | Low | [judgment] |
| P7 | Merge the messages that are the same: `Ack`, `Uri`, `TunnelRequest`, `InstanceState`, a partial `Container` as the availability request | Medium | Low to medium | [sure] |
| P8 | Move Chat and Observe to their own protocols | Medium | Medium | [judgment] |
| P9 | `NoticeDeclaration` becomes a policy | Low | Low | [judgment] |
| P10 | `scope` as bytes; unsigned times; `unknown_formals` in the availability answer | Low | Very low | [sure] |

### 5.3 Each change, and why nothing is lost

**P1. `Signed`.**

```proto
message Signed {
    message Body {
        string context = 1;      // "celaut.Peer", "celaut.EstimatedCost", ...
        bytes signer = 2;        // the public key, as the scheme defines it
        Descriptor scheme = 3;   // one formal
        bytes content = 4;       // the serialized message
    }
    bytes body = 1;              // the exact bytes that are signed
    bytes signature = 2;
}
```

- *Removed:* `Peer.public_key`, `.signature_scheme`, `.signature`;
  `EstimatedCost.signature`; `BalanceStatement.signature`; `ExecutionReceipt.peer_id`,
  `.signature_scheme`, `.signature`; `ReputationProof.publisher`, `.signature_scheme`,
  `.signature`; `Client.peer_id`, `.signature`; and the messages `Peer.SignatureScheme` and
  its `Protocol`.
- *Nothing is lost:* each removed field is in `Body` (signer, scheme) or in `Signed`
  (signature). `context` does the work of the prefixes of today
  (`node_identity.py:60-91`): a signature for one use cannot be read as another. The
  timestamp stays in each content message, because its meaning differs.
- *What is gained:* a node verifies and forwards a message with fields that it does not
  know. A verifier service has one input for every signed message (PENDING 9.4). The
  digest keys leave the layer formal.
- *One detail:* `envs_salt` must not be covered by the signature (its comment says why).
  It moves from the receipt to `ServiceInstance.envs_salt`.
- *Risk:* each path that signs or verifies changes, also in the Rust TUI and in the Peer
  that a node writes to a ledger. A node must keep the bytes that it received. It already
  does this for a Peer (`sql_connection.py:2681-2705`).
- *A cheaper form, if P1 is too large:* keep the messages, and define the signature over
  the received bytes of the message with its signature field removed. This needs the
  canonical encoding for every signed message, and the type changes of P10. It solves
  B1. It does not simplify.

**P2. `terms`.**

- *Removed from `EstimatedCost`:* `launch_reserved`, `price_bounded`, `price_volatility`,
  `volatility_period_seconds`, `closes_at`, `on_close`. *Removed:* the enum `OnClose`, the
  messages `ActivityWindow` and `Span`, and `Peer.activity_window`.
- *Added:* `EstimatedCost.terms` (a requirement for the caller) and `Peer.terms` (an
  announcement).
- *Nothing is lost:* each removed field becomes the value of a term, with a formal that
  defines it. The price bound of today is one formal, and its value is the rate as two
  integers and the period in seconds. That also removes the `float`. The schedule is one
  formal, and its value is the spans. What the node does at a close is one formal.
- *Kept on purpose:* `cost`, `init_maintenance_cost`, `max_maintenance_cost`,
  `maintenance_seconds_loop`, `valid_until`. A balancer compares the offers of two nodes
  with these and with no knowledge of any term.
- *Counter-argument (rule 8.5):* fail closed for every term means that a node with a new
  promise is refused by old callers. The other design is two lists: promises (a caller can
  ignore them) and conditions (a caller must know them). I chose one list. A promise that
  the caller ignores has no value for it. A node that wants to be accepted by all callers
  offers without the new term. If this is a problem in practice, the second list costs one
  field, and it is added before the freeze.

**P3. The catalogs leave the comments.** The comments of `Slot.policies`,
`ItemBranch.policies`, `Filesystem.policies`, `Network.formal`, `Device.architecture`,
`Peer.rates` and `Peer.limits` now say what the list is, its position and its default.
The definitions are already in `docs/policies/`, `docs/NETWORKS.md` and
`docs/payment_protocols/`. No field changes. *Nothing is lost:* the bytes of a formal were
never read from a comment. The node has them in its code, pinned to the files by a test.
I did not shorten the comments of `BalanceMovement.kind` and `.detail`, because they
define text that a client hashes. They should move too, with care.

**P4. `Descriptor`.** It replaces `DataFormat`, `Contract.Ledger`, `Contract.Protocol`,
`Service.Api.Protocol`, `Container.Architecture` and `Peer.Uri.Protocol`. *Nothing is
lost:* all have fields 1, 2 and 3 with the same types, so the bytes of each existing
message are the same, and no service id moves. Only the names change. A name matters in
one place: the layer comparison compares type names as text
(`src/identity/protocol_schema.py:14`, `transport_stack.py:932`). So do this before the
freeze.

**P5. Dead fields.** See C6. *Nothing is lost between nodes:* no node reads them. One
caveat: the packer writes `config_declaration` in every service today. A stored service
keeps its id, and the number 5 is reserved, so a node accepts it and ignores it. A service
that is packed again gets a new id.

**P6. Lists where a real case exists.**

- `Network.policies`: the `pow.` keys were added one by one, and "a reader keeps a key
  that it does not know" is fail open.
- `Configuration.policies`: replaces the reserved key `__nodo_instance_name`.
- `Device.policies`: replaces the enum `Attach`. With no policy, the node attaches the
  device at the start, which is the default of today. `Peer.devices` becomes a list of
  `ArchitectureResources`, which fixes "only at_most and benchmarks have values". *What is
  lost:* a Peer no longer announces which ways to attach it supports. A caller learns it
  from `GetResourceAvailability`, which the comment of today already calls "the real
  answer".
- `ExecutionReceipt.facts`: replaces `kernel` and `devices`.

**P7. Merges.**

- `Ack` replaces `AssociateClientOutput`, `ReputationProofAck` and `ModifyDepositOutput`.
  The bytes are the same (a bool at 1, a text at 2). `IntroducePeer` also returns it, which
  removes the token "REFUSED".
- `Uri` replaces `Instance.Uri` and `Peer.Uri`. `Instance.Uri` is the first two fields of
  `Peer.Uri`, so old bytes still parse.
- `TunnelRequest` replaces `DescendantTunnel` (same field numbers) and the tunnel use of
  `TokenMessage`. `TokenMessage` is then only a token.
- `InstanceState` replaces `ModifyServiceSystemResourcesOutput` (same numbers 1 to 4) and
  `Metrics`. It adds the addresses of the instance (B4). `GetClientMetrics` returns an
  `Amount`.
- `GetResourceAvailability` takes a partial `Service.Container`.
  `ResourceAvailabilityRequest` is removed. Each of its three fields is a field of
  `Container`.

**P8. Chat and Observe.** Each one becomes a file of its own. A node that serves one
announces one more address with its own top layer. I did not write those two files: their
messages move with no change, except that a signed entry or a receipt is a `Signed`.
*Caution:* two stacks are compatible only if they have the same number of layers
(`transport_stack.py:856-859`). So an optional protocol is a second `Uri`, not one more
layer on the same `Uri`. **[not checked]** if one port can serve two gRPC services behind
two announced addresses with no other change.

**P9. `NoticeDeclaration`.** It becomes a policy in `Init.policies`. Its formal names the
system and the format, and its value is the path. *Nothing is lost:* the rule "a node that
does not know it refuses the service" is the rule of the list. Services that declare a
notice get a new id when they are packed again.

**P10. Small types.** `scope` is bytes (the same bytes on the wire for UTF-8 text). Times
are `uint64` (the same bytes for a time that is not negative). `difficulty` and
`maintenance_seconds_loop` are `uint32`. `ResourceAvailability.unknown_formals` is new.

### 5.4 What I looked at and did not change

- **`Network`.** Its tags are data, and that breaks rule 2.2. The firewall, the resolver
  and the pricing all use it. A redesign is a task of its own.
- **`ExecutionReceipt`.** One message for three kinds. A split into a start and a
  checkpoint is correct by rule 4.2. I changed only the envelope and the facts.
- **`ReputationOpinion` and `pow_nonce`.** Content of one model. They are recent (#652,
  #677), and I do not know the plans well enough.
- **`BalanceMovement`.** `kind` and `detail` are open text, and that works. The names of
  the resources in `detail` wait for PENDING 3.
- **The blake2b-256 references** (A6). To make them agile, each node would name the hash
  of its references in a term.
- **`Workload` and its CNF.** Accepted as a mechanism.
- **The field numbers that the branch retyped in place.** See 6.3. I kept the numbers of
  the branch so that the diff stays readable. For my own type changes I used new numbers.

---

## 6. Upgrade mechanics

### 6.1 Three mechanisms decide compatibility today, and they do not agree

| Mechanism | What it compares | A new field in a message | An edited comment |
|---|---|---|---|
| `compat.json` → `nodo update` (`compat.py:57-60`, `update.py:117-119`) | The SHA-256 of the proto file | Not compatible | Not compatible |
| The gateway layer of each address (`transport_stack.py:935-961`) | Keys of a formal; `schema.*` and `rpc.*` can grow | Compatible, if no digest key changes | Compatible (the prose is not compared) |
| The id of a service (`docs/CANONICAL_ENCODING.md`) | The bytes | The old node skips the field | No effect |

So the answer to "can I add a field?" depends on which mechanism you ask. The strictest
one is the one that the operator sees.

**Recommendation.** `compat.json` compares what the layer compares: the wire schema and
the keys that must be equal. Use `formal_conflicts` between the two versions, not a hash
of the file. Then a comment, a new optional field and a new RPC do not stop an update.

### 6.2 Unknown fields

- **In a message with an id** (`Service`, `Filesystem`): the bytes are kept and forwarded.
  A node that *runs* the service refuses an unknown field (rule 3.6). Today it skips it.
- **In a signed announcement:** with `Signed`, the field is covered and kept. Without it,
  the field is not covered, or the digest changes.
- **In a request or an answer that is not signed:** protobuf skips it. That is correct for
  an answer. For a request that asks for something, a node that ignores an unknown field
  does less than the caller asked, with no error. A request has no good extension point
  except a `Policy` list. That is the reason for `Configuration.policies`.

### 6.3 Field numbers

**Rule:** a number has one type and one meaning for ever (rule 4.7 in section 3.13).

The branch changes the type of these fields and keeps their numbers:

| Field | Before | After |
|---|---|---|
| `Container.Resources.at_init = 1`, `.at_most = 2` | `Sysresources` | `repeated Quantity` |
| `ConfigurationFile` field 4 | `Sysresources` | `repeated Quantity` |
| `EstimatedCost` field 5 | `repeated Uint64KeyValue` | `repeated Quantity` |
| `ArchitectureResources.resources = 2` | `Sysresources` | `repeated Quantity` |
| `ResourceAvailabilityRequest` field 1 | `ArchitectureResources` | `Architecture` |
| `ExecutionReceipt` field 11 | `uint64` | `repeated Quantity` |
| `Peer.rates = 7`, `Peer.limits = 8` | key-value lists | `repeated Rate`, `repeated Limit` |
| `ModifyServiceSystemResourcesOutput` field 1, `...Input` field 2 | `Sysresources` | `repeated Quantity` |
| `Metrics` field 2 | `suspended` ("false for a hibernated instance") | `hibernated` (true for it) |

I ran the first case with the two real schemas. A node with the `dev` schema reads a new
`Resources` that has a memory of 512 MiB and a disk of 10 GiB as
`at_init { mem_limit: 10737418240 }`. There is no error. The reason: `Quantity.value` and
`Sysresources.mem_limit` are both field 4 and both a varint, and the last entry wins. A new
`Peer.rates` fails to parse on the old schema, which is the better failure.

The layer comparison stops two such nodes from talking. But a service travels as a file,
and a Peer is stored on a ledger. In alpha this is accepted (`protos/README.md` says "Not
wire compatible, deliberately"). It costs nothing to avoid: give each such field a new
number and reserve the old one. The last row is a change of meaning with no change of
type. No tool can find it.

Also reserve the **names**. The branch writes `reserved 12, 13, 14;` with no names. A name
that is used again breaks the JSON form.

Ranges: I do not recommend a reserved range for extensions. A range invites new fields,
and the goal is that there are none.

### 6.4 Versions of formals

- A formal has no version. Its bytes are its identity. A new meaning is a new formal.
- A formal that replaces another names it (rule 2.6).
- A formal with a list of values (`values=never,on_tunnel`) changes its bytes when a value
  is added. This is correct. It means that an author who wants the new value is refused by
  an old node, and that is what fail closed asks for.
- **The layer formal is the exception.** It changes with each release. Split it (rule
  2.5): one schema formal that grows by addition, and small formals that are compared by
  exact bytes (the references and their hash, the proof of work). After P1, most of the
  keys that must be equal are gone.

### 6.5 The id of a service depends on the version of the packer

When `uses_gateway` is true, the packer writes the whole gateway stack into
`Container.node_protocol_stack` (`zip_with_dockerfile.py:646-648`,
`transport_stack.py:737-751`). That stack includes the `celaut-gateway` formal: the schema
of every message and every digest key. The docstring says "the stack goes into the id of
the service". The node then checks only that one layer has the tag `celaut-gateway`
(`src/virtualizers/microvm/rootfs.py:445-459`).

So:

- The same source, packed with two versions of nodo, gives two service ids, after any
  change to `celaut.proto`, also a compatible one.
- The bytes that cause this are not used for any decision.

The `TODO` in `rootfs.py` describes the fix: the service declares only the RPCs that it
uses. A small and stable formal ("I call `StartService` and `ModifyServiceSystemResources`")
keeps the id stable across releases. This is a change to a formal, not to the contract. It
matters for the goal, because a service id is the thing that must never be forced to
change.

### 6.6 How a change that is not compatible can still happen with no hard fork

The mechanism exists: `Peer.uri` is a list, and each address has its own stack. A node can
announce two addresses with two layer formals, old and new, for a time. Old nodes call the
old one. Three conditions make it work:

1. The Peer must be readable by both. This needs P1: with the digests of today, a node
   cannot write one Peer that verifies on both sides.
2. The identity must stay. A new signature scheme is a new key, and a new key is a new
   identity. The move is a statement, signed by the old key, that names the new key. It
   can be a reputation opinion. It must be published before the old scheme is broken:
   after that, anyone can forge it. This is the one thing that cannot be repaired later.
   **[not checked]** against the reputation code.
3. A service id must not change (6.5).

### 6.7 What must never change after the first release

1. **A field number**, its type and its meaning. A removed number and its name stay
   reserved.
2. **The name of a message**, while the layer comparison compares names, and for each
   signed type, because the name is the `context` of its signature.
3. **The canonical encoding** of a message with an id.
4. **The layout of `Signed` and `Signed.Body`**, and the rule that the signature covers
   the bytes of `body` as they came.
5. **The identity rule:** the exact bytes of a formal. A published formal.
6. **The list rules:** sorted by formal, no repeated formal, and a reader that refuses a
   list that is not sorted in a message with an id.
7. **The position of each list.** A list that is fail closed can never become fail open.
   In the other direction, every node that was accepted yesterday is refused today.
8. **The comparison rule of `Quantity`:** greater than or equal.
9. **`Amount`:** a decimal integer as text, in the MU of the node that issues it.
10. **The convention for a hash type:** the digest of the empty input.
11. **The bee-rpc index of each message in each RPC.**
12. **The text prefixes and the hash of the ledger references** (`celaut-ledger-instance:`,
    `celaut-ledger-account:`, `celaut-balance-movement:`), while they are constants.
13. **The meaning of an absent entry in each list.** An absent policy means the default
    that existed when the list was made. A later formal can never change what "absent"
    means.

### 6.8 A suggested order

1. Now, with no risk: P3 (comments), P10 (types), rule 4.7 for the fields that the branch
   retypes, and the `compat.json` comparison of 6.1.
2. Before the code follows the branch: P4, P5, P7. They are renames and removals, and the
   code changes for the branch at that time.
3. A decision of its own: P1. It is the change that decides if the contract can grow after
   the freeze.
4. With the pricing work (stage 2 of PENDING): P2 and P6.
5. Before the freeze: the check of rule 7.4, and the fix of 6.5.

---

## Appendix: what I did not verify

- I did not run nodo, its tests or the packer. Every claim about behavior comes from
  reading the code at `dev` `f3834aa`.
- I did not read the Rust TUI or the on-chain form of the Peer. P1 changes both.
- "No code of the node reads `config_declaration`" and the other claims of C6 come from a
  search of `src/` for the field names. A read through a dynamic name would not be found.
- The draft pull requests for devices and Bitcoin confirmations (#649, #654) are not on
  `dev`, and I did not read them. P6 changes `Device`, so it must be checked against #649.
- I counted the layer keys with a script over `transport_stack.py:423-585`. I did not
  print the formal of a running node.
- The proposed proto compiles. No code was generated from it, and no test ran against it.
