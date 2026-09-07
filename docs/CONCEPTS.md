# Concepts

A short, self-contained glossary of **celaut**: what a service is, what a node is,
and what the contract between two nodes says. Everything here is paradigm-level —
no term in it depends on how Nodo happens to implement it, and where a concept does
have an implementation in this repository, the pointer says where to read it:
[`USAGE.md`](USAGE.md) for the commands, [`PACKING.md`](PACKING.md) for the packer's
input format, [`CONFIG.md`](CONFIG.md) for configuration,
[`BACKENDS.md`](BACKENDS.md) for execution, [`PRICING.md`](PRICING.md) and
[`ERGO.md`](ERGO.md) for prices and settlement. The paradigm itself is defined in
[celaut-project/paradigm](https://github.com/celaut-project/paradigm); the wire
contract quoted throughout is [`celaut.proto`](../protos/celaut.proto).

## Celaut

A network in which **services** are specialized software components encapsulated in
binary files, and **nodes** are the computers that discover each other, run those
services, and pay each other for the work. It is multi-ledger by design: no ledger is
part of the definition of anything — "not necessarily the only ledger to be used."

## Node

A single participant in the network. A node executes services (locally or by
delegating to peers), exposes a communication interface to the services it runs,
provisions their address + token, and resolves their dependencies. Turning a project
into a specification is explicitly *not* one of a node's responsibilities, though an
implementation may offer it.

## Service specification

A **deterministic, content-addressed** description of a program: its filesystem,
architecture, entrypoint, resource limits, API, and declared environment. It is
identified by the hash of its content (its **service id**), so the same
specification always has the same id, on any node.

It travels in two shapes, and only one of them is a package:

- **A package** — the importable, transmittable artifact: a framed container carrying
  the specification and the blocks it references.
- **A raw specification** — the specification's own bytes, for **hash verification
  only**. It is what the id is the hash of, and it is not importable.

## Instance

A **running** service — one launched specification executing in an isolated
environment. The specification is the blueprint (`service id`); the instance is the
live process (`instance id`). One specification can have many instances.

## Execution environment

A service is a binary that declares the architecture it is built for, so what runs it
is an isolated environment of that architecture, provisioned by the node. celaut names
no virtualization technology and a specification cannot ask for one: a node may boot a
microVM, emulate a foreign architecture, or refuse the work and delegate it, and the
service cannot tell which. Whatever tooling a node uses to *build* a specification is
not what the service runs in either.

The consequence for an operator is that a running instance is observed by asking the
node about it, never by inspecting the host's container or hypervisor tooling — the
node is the only thing that knows which instance a given process is.

## Balances and prices

A node prices each resource on its own — memory, CPU, disk, relayed traffic — and
nothing collapses them into a single number, so a node short on memory but rich in
disk can charge accordingly. Prices rise with contention, up to a ceiling the node
advertises alongside them.

Three things are kept apart, and conflating them is the classic mistake:

| | What it is |
|---|---|
| **MU** (monetary unit) | What a node *counts in*. An integer, so no balance goes through a float. It is the node's own unit of account and has **no intrinsic value**. |
| The **contract rate** | What one MU is *worth*. A property of the payment system, not of MU: each payment contract declares how many MU one of its units buys (`ContractRate.mu_per_unit`), and that declaration travels to peers with every price. |
| The **display unit** | What a human reads and types. Purely presentational, local to one node, absent from the wire; changing it never changes what anybody is charged. |

### Payment systems

A payment system is just a **contract that declares its own rate**. MU is pegged to
nothing, and the accounting core of a node names no ledger: code reading a price is
expected to read the rate rather than assume one.

Two consequences worth stating plainly:

* **A node need not accept the ledger you hold.** Every node advertises the payment
  contracts it accepts (`Peer.payment_contracts`). Paying a peer means finding a
  contract you both hold; a peer that shares none with you will show you its prices
  and be unpayable by you. Sharing at least one is what makes two nodes able to trade
  at all.
* **A node may accept several at once.** What a node advertises is a list, not a
  choice, and which contract settles a given payment is decided per payment by
  matching against what the payer can actually pay with. A pair of nodes sharing more
  than one is ambiguous rather than free to pick — choosing between them is policy,
  and policy is not part of the contract.

Because the rate is declared per contract instead of assumed, a price quoted in MU
stays meaningful to a node that settles in something else entirely.

### Client balances

A client holds a balance with a node, counted in MU. How money becomes balance is the
payment contract's business: the contract defines what a deposit is, where the funds
go, and what proves that a given deposit belongs to a given client. The node's side of
it is the same whatever the ledger — it credits a balance once the contract says the
funds arrived, can quote what an execution will cost before it starts, and lets the
deposit behind a running instance grow or shrink while it runs.

What settles on a ledger is denominated by that ledger, not by anybody's display unit.
The flow for the ledger implemented here is in [`ERGO.md`](ERGO.md), and what things
cost is in [`PRICING.md`](PRICING.md).

## Address and token provisioning

To talk to a running instance you need its **communication address** (`ip:port`, from
the ports the specification declares in its `api`) and an **authentication token**.
Providing these is a core node responsibility. The API's transport, its `protocol`
(e.g. `grpc`) and its `mu_per_call` are declared by the specification itself, so what
a call costs and how it is spoken travel with the service rather than with the node
running it.

## Block

A content-addressed chunk of storage. Large files in a service's filesystem are not
embedded inline in the specification; they are stored as **blocks** referenced by
their content hash, which deduplicates identical large files across services.

## Peers and clients

**Peers** are other nodes a node has connected to; nodes reciprocally offer and
request services from their peers, so a node can run a workload locally or hand it
to a peer. **Clients** are the entities (nodes or external callers) that have
registered with a node and pay it.

A peer is named by its identity public key — see [Node identity](#node-identity).

## Transport security

Every hop between nodes is TLS. A node's certificate is self-signed and carries the
node's identity public key — its `peer_id` — in an X.509 extension, signed with the
identity key over the certificate's own public key. There is no CA, no PKI and no
system trust store: a caller reads the certificate first, checks that signature, and
then pins that exact certificate for the channel. So dialling a bare `ip:port` either
reaches the node whose `peer_id` you meant, or fails. Only an authenticated address is
announced to peers, and a peer channel has no plaintext fallback.

Two hops are deliberately outside it, and neither is a hop between nodes:

* **The node ↔ service hop.** A service reaches its node over a hop that never leaves
  the host, and it is handed that address as data, so there is nothing for it to guess.
  Requiring TLS there would mean shipping certificate pinning into every service SDK
  for a local hop.
* **A caller that declines TLS.** TLS is what a node *offers*; whether it also answers
  an unauthenticated address is that node's own policy and that caller's own risk. Such
  an address is not announced to peers — serving the same interface unauthenticated on
  every interface would give away exactly what the authenticated one protects.

Also outside TLS: the node→service leg of a tunnel, where TLS terminates at the node
(see [`TUNNELING.md`](TUNNELING.md)), and the raw TCP proxy of the delegation path,
which is not gRPC.

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
compiled descriptor rather than typed out — so adding or removing one changes what a
node announces without anyone remembering to.

That makes the claim checkable, by the same component-stack comparison a signature
scheme gets (see [The signature scheme is declared, not
assumed](#the-signature-scheme-is-declared-not-assumed)): a peer differing in the OID,
in the signed payload or by a single RPC is seen as speaking something else, while one
that only worded its prose differently is not.

**Prose is deliberately not compared.** Deciding that two differently-worded
descriptions mean the same protocol is a judgement, and the service that could make it
has the shape `(a, b) -> bool` over two texts — an LLM's job, not a node's. Prose travels
so the descriptor can be *read*, not diffed. It is dropped from an announcement published
to a ledger register, where every byte pays storage rent forever; nothing is lost from a
verification, because what a comparison reads is `formal` and the tags.

A full announcement runs to roughly 5 KB per advertised address, so it is **signed once
per change rather than once per caller**: the announcement RPC serves a cached,
byte-identical answer until the content actually changes. Nothing is given up for it —
the signature is over a public object, and `ts` guards only against a downgrade to a
stale address, so nothing about the caller was ever in what was signed. A repeated `ts`
also lets the *receiver* skip a full refresh, including the on-chain revalidation of the
proofs the announcement carries.

The OID itself is `uuid.uuid5(uuid.NAMESPACE_OID, "CELAUT")` written under the ITU-T
X.667 arc `2.25`, which anyone may derive from a UUID with nothing to register. The seed
is the project name and nothing more, so the number can be recomputed in one line and
audited — it is a name seed, never a resource, and only the number ever travels.

## Node identity

A node's **id is its identity public key**; there is no other name for it. Every
announcement (`Peer`) carries that key, a signature over everything the peer
advertises, and the cryptography those two are in. A peer that carries no key, or
whose signature does not verify, is refused outright — there is nothing else to
register it under. The key is derived from a seed its operator holds, so an identity
cannot change underneath the peers that recorded it, and a node with no identity
keypair can neither serve nor dial.

### The identity is on no ledger

The obvious shortcut is to let a ledger key *be* the identity — sign with the node's
wallet, and let a reputation proof's R7 owner be literally the `peer_id`. It makes the
check a byte comparison. It is deliberately not done, and the reason is in the
contract. R7 is the reputation contract's spending clause:

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
R9  = Peer{ public_key: <identity>,   │   ← the attestation
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
cryptography is a comparison — the one place an equivalence service of the shape
`(scheme_a, scheme_b) -> bool` would be asked instead.

How a node compares two schemes on its own, until such a service is asked, is a
one-to-one pairing between their components — every component on each side paired with
exactly one on the other, order carrying no meaning — where each pair is decided by:

* **`formal`, when both sides of the pair carry one.** Stating the parameters is the
  strictest identity a component has, and two components stating different ones name
  different things however their tags read. An `ed25519` component can state them
  (`key=value` lines, sorted, UTF-8); a ledger convention's `formal` may stay empty
  because there is nothing determinate to state.
* **One shared tag, otherwise.** The tags of a component are alternative names for the
  single thing it names, so agreeing on any one of them is agreeing on the thing:
  `["tls", "tls1.3"]` and `["tls1.3", "tls-1.3"]` are one protocol under two
  vocabularies, and demanding the whole set match would refuse a peer for spelling it
  differently. Where a shared tag is too weak a conclusion — `["ed25519",
  "ed25519ph"]` names the pre-hashed variant of RFC 8032 beside the pure one, and its
  signatures do not verify under the pure procedure — `formal` is how a component says
  precisely what it is, and then it decides.
* **A `formal` on one side alone does not decide.** Stating the parameters says more
  than staying quiet about them; it does not contradict a peer that stayed quiet, so
  the tags still answer. Otherwise pinning a component down would cut a node off
  from everyone naming the same thing without pinning it.
* **Nothing at all, never.** A component must carry `tags`, `formal` or both. One
  holding only `prose` — or nothing — is not a building block a node can reason
  about, so the scheme is refused rather than half-compared. For the same reason a
  component carrying only `formal` shares nothing with one carrying only tags.
* **`prose`, never.** It is human text with no agreed wording, and making it decisive
  would refuse a peer for rewording a sentence. What it is for is being read: it says
  in words what `formal` states as parameters, complete enough to implement the
  verification from.

Matching on one shared tag makes the relation **non-transitive**: `[a,b]` matches
`[b,c]` matches `[c,d]`, and the ends do not match. It identifies components; it does
not partition them into classes, and nothing may group by it.

The search for that pairing is factorial in the number of components, which is a number
the *peer* chooses, so an implementation caps it: a longer scheme is refused rather than
computed. Comparing against a single-component scheme of one's own is bounded by the
cardinality check regardless; the cap is what keeps that true if two peers' schemes are
ever compared to each other.

Across the whole scheme, though, the pairing must be total: a peer declaring
`["secp256k1"]` and `["bip340"]` as two components shares the curve component with a
node declaring `["secp256k1"]`, `["schnorr"]`, `["blake2b256"]` and `["ergo"]`, and still
produces signatures that node cannot read — same cardinality or not, a partial match is
not a shared scheme.

An empty descriptor (no components at all) means the sender's default, so an
announcement predating the field still verifies. A node declaring a scheme its reader
does not implement is refused unread, rather than reported as a bad signature.

### One identity, many ways to pay

What is singular and what is plural is deliberate, and the two do not conflict:

| Field | Count | Why |
|---|---|---|
| `public_key`, `signature`, `signature_scheme` | one | The key is what **names** the node, so a second one at the same level is a second identity: reputation, deposits and payment attribution all split in two. Cross-signing two *names* does not heal the split — whoever needs the link speaks only one of the schemes, so they can verify only half of the proof. |
| owner attestation, in each proof's `xattrs` | one per proof | The wallet that **published that proof**, and its signature over the node's id. Not a second name: it vouches *for* the identity above, so there is a single root and nobody has to pick which key the node is. |
| `payment_contracts` | many | What a node accepts is a **menu the payer picks one item from**, so a longer one costs nothing. Being named by a key of its own while accepting several ledgers is the expected shape. See [Balances and prices](#balances-and-prices). |
| `reputation_proofs` | many | A node holds as many proofs as it has published opinions under. See [Reputation proof](#reputation-proof). |

The distinction that runs through the table is **root versus role**. A key in a
different role, signed by the identity, is not a competing name: that is what a ledger
owner attestation is, and what the transport certificate's own key is (see
[Transport security](#transport-security)). One root, several keys under it. What must
stay singular is the root.

A scheme that genuinely needs two keypairs — a classical/post-quantum hybrid — is *one*
scheme, whose key and signature encodings carry both, and not two schemes on one peer.

Only *signing* is singular, though. What a node can **verify** is a local capability:
the way to reach a peer that signs differently is to plug a verifier for that scheme
into the reader, never to ask the peer to carry more keys. So a node that changes its
scheme becomes a new peer to everyone who cannot read the new one, with the reputation
of one.

## Service composition (dependencies)

Services can depend on other services, and a dependency is named the way everything
else is: by content hash. So a service addresses its dependencies by service id
regardless of which node ends up running them, and resolving an id to a reachable
address is the node's job — the fourth of its responsibilities. What the dependent
service is given is an address and a token, exactly as any caller would be.

## Core services

Celaut services a node treats as part of its own workflow, referenced by service id
rather than built in. The well-known roles are `packer` (builds services in a sealed,
isolated environment), `source-application` (resolves a service id to its downloadable
sources), and `low-demand-fallback` (an opportunistic service run only when the node is
idle). A node's own machinery being celaut services means it is replaceable, and
replaceable by id.

## Reputation proof

An on-chain record (in the ledger implemented here, a token held in "reputation boxes")
through which a node publishes **its own opinions about other nodes**. Each box is one
opinion: register R5 names the node it is about — by that node's **identity public
key**, the same key that is its `peer_id` — and the token amount in the box is the
weight behind it.

Read the direction carefully: a proof belongs to its *author*. `Peer.reputation_proofs`
in an announcement is what that peer thinks of others, never a rating of the peer
itself, and a single identity key may hold several proofs at once. What a node thinks of
a peer is separate and local: a score plus the events that explain it, keyed by the
peer's public key.

It lets peers assign each other trust in a decentralized, transparent way. Nodes
generate and submit these proofs; publishing celaut node/service entities uses the same
reputation-box machinery. Model: [`ERGO.md`](ERGO.md).

What a node publishes about another node is an opinion, so it is only as strong as
what backs it. An [execution receipt](EXECUTION_RECEIPTS.md) is what a client can back
one with: a statement the accused node signed itself, about one execution it ran.

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
