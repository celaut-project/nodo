# Concepts

A short, self-contained glossary of the terms Nodo uses. It is the conceptual
companion to the task-oriented [`USAGE.md`](USAGE.md), the [`PACKING.md`](PACKING.md)
input format, and the payment/reputation model in [`ERGO.md`](ERGO.md). The
underlying paradigm is defined in
[celaut-project/paradigm](https://github.com/celaut-project/paradigm).

## Celaut

The paradigm Nodo implements: a network in which **services** are specialized
software components encapsulated in binary files, and **nodes** are the computers
that discover each other, run those services, and pay each other for the work. It
is designed to be multi-ledger; Ergo is the ledger implemented today (see
[`ERGO.md`](ERGO.md)) — "not necessarily the only ledger to be used."

## Node (`nodo`)

A single participant in the network. A node executes services (locally or by
delegating to peers), exposes a communication interface to the services it runs,
provisions their address + token, resolves their dependencies, and — in this
implementation — can pack projects into service specifications. See the node
responsibilities in the project [`README.md`](../README.md).

## Service specification

A **deterministic, content-addressed** description of a program: its filesystem,
architecture, entrypoint, resource limits, API, and declared environment. It is
identified by the hash of its content (its **service id**), so the same
specification always has the same id, on any node. Produced by `nodo pack` (see
[`PACKING.md`](PACKING.md)); exchanged as a `.celaut.bee` package.

- **`.celaut.bee`** — the importable/transmittable package (`nodo export <svc> <dir>`).
- **`.celaut` (raw)** — a raw specification for **hash verification only**; it is
  **not** importable (`nodo export <svc> <dir> --raw`).

## Instance

A **running** service — one launched specification executing inside a microVM.
The specification is the blueprint (`service id`); the instance is the live
process (`instance id`). One specification can have many instances. `nodo execute`
creates an instance; `nodo instances` lists them; `nodo kill` stops one.

## microVM (Cloud Hypervisor, `ch`)

Services **execute** inside isolated Cloud Hypervisor microVMs — not Docker
containers. `ch` is the only virtualizer. Docker, when used at all, is only for
the *packing* step (and only in the opt-in `packer.local` mode); it never runs a
service. Do not use `docker ps` to inspect a running instance — use
`nodo instances` / `nodo observe`.

## Balances and prices

A node prices each resource on its own — memory, CPU, disk, relayed traffic — and
nothing collapses them into a single number, so a node short on memory but rich in
disk can charge accordingly. Prices rise with contention, up to a ceiling the node
advertises alongside them.

Three things are kept apart, and conflating them is what the previous "gas" model got
wrong:

| | What it is |
|---|---|
| **MU** (monetary unit) | What the node *counts in*. An integer, so no balance goes through a float. It is the node's own unit of account and has **no intrinsic value**. |
| The **contract rate** | What one MU is *worth*. A property of the payment system, not of MU: each payment contract declares how many MU one of its units buys, and that declaration travels to peers with every price. |
| `ui.DISPLAY_UNIT` | What *you* read and type. Purely presentational; changing it never changes what anybody is charged. |

### Where ERG fits

Ergo is the **default** payment system and ERG the default representation. Neither is
fixed, and neither is part of the definition of anything. A payment system is just a
contract that declares its own rate; Ergo is simply the first one implemented
(`src/payment_system/contracts/ergo/`), and it sits beside a simulated contract used
for testing. The accounting core names no ledger — MU is not pegged to ERG, and code
reading a price is expected to read the rate rather than assume one.

Two consequences worth stating plainly:

* **Another node need not accept ERG.** Every node advertises the payment contracts it
  accepts (`Peer.payment_contracts`). Paying a peer means finding a contract you both
  hold; a peer that shares none with you will show you its prices and be unpayable by
  you. Sharing at least one is what makes two nodes able to trade at all.
* **A node may accept several at once**, ERG among them or not. What a node advertises
  is a list, not a choice, and which contract settles a given payment is decided per
  payment by matching against what the payer can actually pay with.

Because the rate is declared per contract instead of assumed, a price quoted in MU
stays meaningful to a node that settles in something else entirely.

### Topping up

The flow below is Ergo's, being the payment system currently implemented; another
contract would define its own equivalent.

A client tops up its balance on a node by generating a **deposit token** — a
locally-generated identifier, not an on-chain asset — and submitting an Ergo
transaction that carries that identifier (in register R4) plus some ERG; the node
verifies the deposit belongs to the client and that the funds reached its wallet,
then credits the balance. Nodes run a single hot wallet
(`ledgers.ergo.WALLET_MNEMONIC`); clients pay its derived P2PK address, and excess
is swept to an optional cold address.

`nodo estimate` reports what a service costs before you run it, and
`nodo increase_deposit` / `nodo decrease_deposit` adjust a running instance — all in
your display unit, ERG unless you changed it. `nodo pay` is the exception and always
takes ERG: what it moves is an on-chain ERG transfer, denominated by the ledger rather
than by a display preference. Full model: [`PRICING.md`](PRICING.md) for what things
cost, [`ERGO.md`](ERGO.md) for how they settle.

## Address and token provisioning

To talk to a running instance you need its **communication address** (`ip:port`,
from the ports the service declares in `service.json → api`) and an
**authentication token**. Providing these is a core node responsibility
(project [`README.md`](../README.md)); the API's transport, `protocol` (e.g.
`grpc`) and `mu_per_call` come from the service's own `service.json → api`
block (see [`PACKING.md`](PACKING.md)).

## Block

A content-addressed chunk of storage. Large files in a service's filesystem are
not embedded inline in the protobuf; they are stored as **blocks** referenced by
their content hash, which deduplicates identical large files across services. See
*Filesystem Parsing Behaviour* in [`PACKING.md`](PACKING.md).

## Peers and clients

**Peers** are other nodes this node has connected to; nodes reciprocally offer and
request services from their peers, so a node can run a workload locally or hand it
to a peer. **Clients** are the entities (nodes or external callers) that have
registered with this node and pay it. `nodo peers` / `nodo clients` list them.

A peer is named by its identity public key — see [Node identity](#node-identity).

## Transport security

Every gRPC hop is TLS. A node's certificate is self-signed and carries the node's
identity public key — its `peer_id` — in an X.509 extension, signed with the identity
key over the certificate's own public key. There is no CA, no PKI and no system trust
store: a caller reads the certificate first, checks that signature, and then pins that
exact certificate for the channel. So dialling a bare `ip:port` either reaches the node
whose `peer_id` you meant, or fails.

**Peers and the CLI always use TLS, with no exception**, and the TLS port
(`network.GATEWAY_PORT`) is the only one announced to peers. This node's own client code
has no way to open a plaintext channel.

Alongside it the gateway serves the **same** `Gateway` on a second, plain-gRPC port
(`network.GATEWAY_PLAINTEXT_PORT`, `auto` = `GATEWAY_PORT + 1`) for two callers that are
not peers:

* **The services this node executes.** A service speaks plain gRPC and reaches the node
  over a hop that never leaves the host; it is handed this address as data, in
  `__config__.gateway`, so there is nothing for it to guess. Requiring TLS here would
  mean shipping certificate pinning into every service SDK for a local hop.
* **External callers that do not want TLS.** TLS is what the node *offers*; a caller
  that declines it is that caller's own risk. The plaintext port is not announced to
  peers, no firewall rule is opened for it, and it listens on one address only — the
  gateway address the config file already names (`virtualizers.ch.NETWORK_BRIDGE_NAME`,
  the same one written into `__config__.gateway`; loopback if that bridge is not up),
  never `[::]`. Serving the unauthenticated `Gateway` on every interface would give away
  exactly what the TLS port protects. So reaching it from another host takes a
  port-forward set up on purpose. `0` disables it, and then a service must speak TLS too.

Also outside TLS: the node→service leg of a tunnel (TLS terminates at the node, see
[`TUNNELING.md`](TUNNELING.md)) and the raw TCP proxy of the delegation path, which is
not gRPC.

### What `["tls", "grpc"]` names

A tag on its own says almost nothing: two nodes can both write `tls` and disagree on the
extension OID, on what the signature covers, or on which RPCs exist — and neither could
tell from the announcement. celaut has no conventions to fall back on, so an address that
announces this stack declares what it means by it, in the three fields every replaceable
component in celaut carries:

| Field | Carries | Compared? |
|---|---|---|
| `tags` | The plain protocol name — `tls`, `grpc`. Not versioned: a variant is the same protocol with different parameters, and those go below. | Only when neither side declares `formal` |
| `formal` | The parameters, as canonical `key=value` lines sorted by key. | **Yes — decides** |
| `prose` | The same thing written out, with the detail an implementer needs. | **No** |

So the announcement for `tls` carries, among others:

```
host_key_oid=2.25.276125094420857322236898758448456352855
host_key_signed=CELAUT<subject_public_key_info_der_hex>
host_key_extension=ascii:<identity_public_key_hex>:<signature_hex>
verification=read-certificate,verify-extension,pin-exact-certificate
```

and `grpc` carries the service and **every RPC the gateway answers**, read from the
compiled descriptor rather than typed out — so adding or removing one changes what this
node announces without anyone remembering to.

That makes the claim checkable. `speaks_our_transport_stack` runs the same comparison a
signature scheme gets (`node_identity.same_component_stack`): a peer differing in the
OID, in the signed payload or by a single RPC is seen as speaking something else, while
one that only worded its prose differently is not.

**Prose is deliberately not compared.** Deciding that two differently-worded
descriptions mean the same protocol is a judgement, and the service that could make it
has the shape `(a, b) -> bool` over two texts — an LLM's job, not a node's. Prose travels
so the descriptor can be *read*, not diffed. It is dropped from an announcement published
to an Ergo register, where every byte pays storage rent forever; nothing is lost from a
verification, because what a comparison reads is `formal` and the tags.

A full announcement runs to roughly 5 KB per advertised address, so it is **signed once
per change rather than once per caller**: `GetPeerInfo` serves a cached, byte-identical
answer until the content actually changes. Nothing is given up for it — the signature is
over a public object, and `ts` guards only against a downgrade to a stale address, so
nothing about the caller was ever in what was signed. A repeated `ts` also lets the
*receiver* skip a full refresh, including the on-chain revalidation of the proofs the
announcement carries.

The OID itself is `uuid.uuid5(uuid.NAMESPACE_OID, "CELAUT")` written under the ITU-T
X.667 arc `2.25`, which anyone may derive from a UUID with nothing to register. The seed
is the project name and nothing more, so the number can be recomputed in one line and
audited — it is a name seed, never a resource, and only the number ever travels.

Practical consequences: a node with no identity keypair cannot serve, and a peer running
a version from before this cannot be dialled — peer channels have no plaintext fallback.
See `src/identity/tls_identity.py` and `src/identity/grpc_transport.py`.

## Node identity

A node's **id is its identity public key**; there is no other name for it. Every
announcement (`Peer`, in `celaut.proto`) carries that key, a signature over everything
the peer advertises, and the cryptography those two are in. A peer that carries no
key, or whose signature does not verify, is refused outright — there is nothing else
to register it under. The key is Ed25519, derived from `identity.MNEMONIC`, so an
identity cannot change underneath the peers that recorded it.

### The identity is on no ledger

The obvious shortcut is to let a ledger key *be* the identity — sign with
`ledgers.ergo.WALLET_MNEMONIC`, and let a reputation proof's R7 owner be literally the
`peer_id`. It makes the check a byte comparison. It is deliberately not done, and the
reason is in the contract. R7 is the reputation contract's spending clause:

```ergoscript
INPUTS.exists { b.propositionBytes == SELF.R7[Coll[Byte]].get }
```

So R7 holds an Ergo proposition and can hold nothing else, ever. An identity read out of
it is fixed as an Ergo key by construction, for every celaut node — which privileges one
ledger's reputation system over any other, and makes Ergo a dependency of the
peer-to-peer layer, down to a node with no wallet being unable to serve or dial at all.

What links the two is an **owner attestation**: the wallet that published a proof signs
the node's `peer_id`, and the pair rides in that proof's own `xattrs`
(`owner_public_key`, `owner_signature`). A reader checks two links instead of comparing
bytes:

```
R7  = the attested wallet (owner, and the only key that can spend the box)
       │  signs peer_id ──────────────┐
R9  = Peer{ public_key: <ed25519>,    │   ← the attestation
            signature, ts }  ◄────────┘
       │  signed by the identity key
       ▼
   addresses, expiry, anti-replay, payment contracts, proofs
```

Both links are verifiable from the proof box alone, with no round-trip to the node, so
the indirection costs nothing a direct byte comparison would have saved. What it buys is
that the node's name outlives its wallets: adding, dropping or rotating one leaves its
peers, deposits and reputation intact, and a second ledger's reputation system attaches
the same way without either being privileged.

Note what R5 does, by contrast: it names the *subject* of an opinion, is plain
`Coll[Byte]`, and so carries the identity key of the node being talked about —
whatever cryptography that identity is in. R5 and R7 are not the same kind of thing,
and only R7 is constrained by what the contract has to be able to spend.

It lives on the proof rather than on the `Peer` because **ownership is a property of the
proof**. A node holds as many proofs as it likes and nothing says they share an owner, so
one attestation per ledger on the announcement could not describe two proofs on the same
ledger published by different wallets. Riding in the proof also means the announcement's
signature already covers it, through `reputation_proofs`.

An attestation proves possession of a key, and nothing more. It does not say the node
accepts payment on that ledger (`payment_contracts` does, and it carries its own address)
nor that the key holds funds. A proof whose attestation does not verify is treated as
announcing no owner at all: the peer's identity is untouched, and only what a reader
would have credited for that proof is lost.

### The signature scheme is declared, not assumed

`Peer.signature_scheme` is an open, unordered stack of components
(`Peer.SignatureScheme.components`) — one per building block (curve, signature
algorithm, challenge hash, ledger convention, ...) — rather than four fixed named
fields, so a future scheme with a different shape (hash-based, threshold, no curve at
all) needs no proto migration to be expressed, only a different-length stack. Each
component is a `tags` / `prose` / `formal` descriptor, the same shape a ledger
(`Contract.Ledger`), an address's transport (`Peer.Uri.Protocol`) or a container
architecture is declared with. Nothing derives an id from it, here or anywhere else in
celaut: a hash algorithm can name itself as `H("")` because hashing is keyless and
unary, but verification takes a key, a message and a signature and has no such
canonical output. **The descriptor is the name**, and whether two of them mean the same
cryptography is a comparison — `node_identity.same_signature_scheme`, which is also the
single place an equivalence service of the shape `(scheme_a, scheme_b) -> bool` would
be asked instead.

How a node compares two schemes on its own, until such a service is asked, is a
one-to-one pairing between their components — every component on each side paired with
exactly one on the other, order carrying no meaning — where each pair is decided by:

* **`formal` first.** A machine-readable specification is the strictest identity for
  that one component. Nothing publishes one yet, so every component of this node's own
  scheme has an empty `formal` — exactly as the Ergo ledger's is.
* **The tags as an exact set** when neither side of the pair has a `formal`. Not an
  intersection: the tags within one component are *meant* to be synonyms for the one
  thing it names (`["secp256k1", "K-256"]`), but nothing in the message says so, and a
  node cannot tell a restatement from a second, different claim — `["schnorr",
  "bip340"]` looks exactly like `["secp256k1", "K-256"]` from here. One of the two
  guesses accepts a signer whose signatures this node cannot verify, so an extra tag
  makes it a different component. `formal` is the way out of that rigidity: a component
  that points at a specification is decided by the specification, and its vocabulary
  stops mattering.
* **Nothing at all, never.** A component must carry `tags`, `formal` or both. One
  holding only `prose` — or nothing — is not a building block this node can reason
  about, so the scheme is refused rather than half-compared.
* **`prose`, never.** It is human text with no agreed wording, and making it decisive
  would refuse a peer for rewording a sentence. What it is for is being read: while
  `formal` is empty, that paragraph *is* the specification of that building block,
  written to be enough to implement the verification from.

The search for that pairing is factorial in the number of components, which is a number
the *peer* chooses, so `communication.MAX_SIGNATURE_SCHEME_COMPONENTS` (5 by default)
caps it: a longer scheme is refused rather than computed. Comparing against this node's
own four-component scheme is bounded by the cardinality check regardless; the cap is
what keeps that true if two peers' schemes are ever compared to each other.

Across the whole scheme, though, the pairing must be total: a peer declaring
`["secp256k1"]` and `["bip340"]` as two components shares the curve component with a
node declaring `["secp256k1"]`, `["schnorr"]`, `["blake2b256"]` and `["ergo"]`, and still
produces signatures that node cannot read — same cardinality or not, a partial match is
not a shared scheme.

An empty descriptor (no components at all) means the sender's default, so an
announcement predating the field still verifies. This node speaks Schnorr over
secp256k1 in Ergo's off-chain encoding and nothing else: an announcement declaring
another scheme is refused unread, rather than reported as a bad signature.

### One identity, many ways to pay

What is singular and what is plural is deliberate, and the two do not conflict:

| Field | Count | Why |
|---|---|---|
| `public_key`, `signature`, `signature_scheme` | one | The key is what **names** the node, so a second one at the same level is a second identity: reputation, deposits and payment attribution all split in two. Cross-signing two *names* does not heal the split — whoever needs the link speaks only one of the schemes, so they can verify only half of the proof. |
| owner attestation, in each proof's `xattrs` | one per proof | The wallet that **published that proof**, and its signature over this node's id. Not a second name: it vouches *for* the identity above, so there is a single root and nobody has to pick which key the node is. |
| `payment_contracts` | many | What a node accepts is a **menu the payer picks one item from**, so a longer one costs nothing. Being named by a key of its own while accepting ERG, bitcoin and anything else that settles is the expected shape. See [Balances and prices](#balances-and-prices). |
| `reputation_proofs` | many | A node holds as many proofs as it has published opinions under. See [Reputation proof](#reputation-proof). |

The distinction that runs through the table is **root versus role**. A key in a
different role, signed by the identity, is not a competing name: that is what a ledger
an owner attestation is, and what the TLS certificate's per-process P-256 key is (see
[Transport security](#transport-security)). One root, several keys under it. What must
stay singular is the root.

A scheme that genuinely needs two keypairs — a classical/post-quantum hybrid — is *one*
scheme, whose key and signature encodings carry both, and not two schemes on one peer.

Advertising several payment contracts is not the same as settling in several: which
one pays for a given interaction is matched per payment, and a pair of nodes that
happens to share more than one is refused as ambiguous rather than chosen between
(`mu_conversion.matching_payment_system`) — picking is policy nobody has written yet.

Only *signing* is singular, though. What a node can **verify** is a local capability:
the way to reach a peer that signs differently is to plug a verifier for that scheme
into the reader, never to ask the peer to carry more keys. Nothing plugs one in today,
so in practice two nodes must speak the same scheme to register each other, and a node
that changes its scheme becomes a new peer, with the reputation of one.

## Service composition (dependencies)

Services can depend on other services. In `pack_config.json` you declare
`dependencies`; with `dependencies_env: true` the packer injects each resolved
dependency's content hash into the build as an environment variable, so a service
can address its dependencies by id regardless of which node runs them. See the
`dependencies` / `dependencies_env` reference in [`PACKING.md`](PACKING.md).

## Core services

Celaut services the node treats as part of its own workflow, referenced by service
id (content hash) in `core_services` in `config.yaml`. The well-known roles today
are `packer` (builds services in a sealed microVM for `nodo pack`),
`source-application` (resolves a service id to its downloadable sources), and
`low-demand-fallback` (an opportunistic service run only when the node is idle;
WIP). See [`CONFIG.md`](CONFIG.md).

## Reputation proof

An on-chain record (an Ergo token held in "reputation boxes") through which a node
publishes **its own opinions about other nodes**. Each box is one opinion: register
R5 names the node it is about — by that node's **identity public key**, the same key
that is its `peer_id` — and the token amount in the box is the weight behind it.

Read the direction carefully: a proof belongs to its *author*. `Peer.reputation_proofs`
in an announcement is what that peer thinks of others, never a rating of the peer
itself, and a single identity key may hold several proofs at once (issue #281). What
we think of a peer is separate and local: `peer.reputation_score` plus the
`reputation_events` that explain it, keyed by the peer's public key.

It lets peers assign each other trust in a decentralized, transparent way. Nodes
generate and submit these proofs; publishing celaut-node/service entities
uses the same reputation-box machinery. Model: [`ERGO.md`](ERGO.md).

## Coverage / Benchmark / Result / Skill

The four on-chain entity types in the **Unstoppable Skills** registry
([celaut-project/skills](https://github.com/celaut-project/skills)):

- **Skill** — a *problem* marker (e.g. "Optimal XAU/BTC Performance").
- **Coverage** — a service that addresses (covers) a Skill.
- **Benchmark** — a deterministic spec for how to measure a Skill.
- **Result** — a comparative measurement submitted against a Benchmark.

Agents "search for problems, not servers": pick a Skill, then read its Coverages,
Benchmarks and Results to choose a service. These are read via the Celaut Skills
MCP server; see the bridge skill in [`skill/SKILL.md`](skill/SKILL.md).
