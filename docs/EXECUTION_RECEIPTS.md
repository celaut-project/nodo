# Execution receipts

A **receipt** is a signed statement by a node that, on itself, a given service ran
with given environment variables and moved a given stream of bytes in and out. It is
signed with the node's Ed25519 identity key, so it verifies directly against the
`peer_id` it is about (see [Node identity](CONCEPTS.md#node-identity)).

Its purpose is narrow and worth stating before anything else: **it gives a client
something to denounce a node with**, and it gives the reputation system something
better than an unsupported opinion to carry. Nothing here makes a node's execution
verifiable, and nothing here protects the node.

---

## The asymmetry this exists to correct

A client pays a node up front. From that moment the node holds the money, the machine
and the only account of what happened; the client holds a promise. What keeps the
arrangement honest is not the payment — that is already spent — but the node's
reputation, which it stakes and which is public
([`ERGO.md`](ERGO.md#reputation-system-implementation)).

That stake is only as good as the evidence a client can bring against it. Today a
client that is overcharged, cut off, or served something other than the service it
asked for can say so and nothing more; a reader of the reputation graph has one
party's word against another's, and no way to tell a defrauded client from a
malicious one. A receipt closes that gap in the only way that needs no arbiter: the
accused signs the evidence himself.

## Unilateral by decision

A `client_id` is a plain identifier issued by each node (`celaut.proto`'s `Client`,
`Gateway.GenerateClient`, the `clients` table). It carries no key, and this document
assumes it stays that way. Clients presenting an identity of their own — a `peer_id`,
like nodes do since issue #236 — is deferred, not rejected; see
[What a client identity would add](#what-a-client-identity-would-add).

Three consequences follow, and the whole design is shaped by them.

**A receipt binds the node and nobody else.** The node cannot prove the client asked
for those envs, agreed to that price, or received that output. If a client publishes
some receipts and omits others, the node has no signed counter-record to point at.

**Receipts are therefore negative-only evidence.** A node cannot build standing out
of its own receipts. `client_id`s live in the node's own namespace, so a node can
invent as many clients as it likes and sign a flawless history for all of them; a
self-published set of good receipts is a document a node wrote about itself, start to
finish. Only a receipt held *against* its signer carries information. Nothing in the
reputation model should ever read receipt volume, or an absence of complaints, as a
positive signal.

**Publication is selective, and that is acceptable.** A client publishes the receipts
that damn the node and keeps the rest. The published set is true but not
representative, because a receipt cannot be forged — only chosen. So a receipt is
*evidence*, never a *statistic*: it supports a claim about one execution and must not
be aggregated into a rate. Ten complaints citing ten receipts are ten facts about ten
executions, not a failure percentage.

---

## What a receipt asserts, and where each part comes from

| Claim | Source | Status |
| --- | --- | --- |
| ran on `peer_id` | the identity key *is* the `peer_id` (`src/identity/node_identity.py`), so the signature is the claim | already available |
| ran `service_id` | content-addressed (`Metadata.HashTag.Hash`), verified by the node (`manager/integrity.py`, `utils/service_content.compute_id`), persisted in `local_instances.service_id` | already available |
| with `envs` | `Configuration.environment_variables`, persisted in `local_instances.envs` | needs a canonical digest |
| and this cable state | the tunnel relay and the observability tap | the real work |

### The envs need a canonical digest of their own

`local_execution._serialize_envs` already writes a sorted-key JSON of the env map,
but it decodes values with `errors="replace"`. That is right for a column meant to be
read by a human and wrong for anything signed: two different env maps can serialise
to the same text, so a digest over it would attest to something weaker than it
appears to. A receipt digests the raw `bytes` of each value, field by field, the way
`canonical_peer_content_digest` is built and for the same stated reason — protobuf
serialisation is not canonical, so nothing signed is derived from
`SerializeToString()`.

The **effective** envs are what must be signed, not only what the client sent: the
node injects network resolution and dependency wiring of its own. A receipt that
digests only the client's half would be attesting to a configuration the service
never ran under. It records both, and says which half came from the client, so a
reader can tell an injected variable from a requested one.

### An execution is named by a digest, never by its token

An instance's id doubles as a bearer credential: possession of the token is what
authorises a tunnel to it and what its balance is spent against (`TrafficMeter`,
`Gateway.GetMetrics`). Publishing a receipt that contains it would hand every reader
tunnel access to the instance and the ability to drain its balance. A receipt names
the execution by a digest of that token together with the node's key, so the name is
stable, unlinkable to the credential, and still recognisable to both parties, who each
hold the token.

---

## Cable state: four planes, four different strengths

"What moved in and out" is not one thing. What the node can honestly attest depends
on where it sits relative to the bytes, and a receipt must **declare its coverage**
rather than let a reader assume the strongest case.

**The tunnel (`Gateway.ServiceTunnel`).** Here the node *is* the cable: `_relay` and
`_pump_to_service` in `src/tunneling/rpc_tunnel.py` see every byte in both
directions, and `TrafficMeter.add` already counts them to bill for them. This is the
strong case, and the cheap one — the bytes are already in hand and already copied, so
hashing them adds a pass over data that is in cache.

**The observability tap (`Gateway.Observe`).** `ObserveEvent.Packet` carries the
verbatim frame and its kernel timestamp, so a byte-exact `.pcap` can be rebuilt
remotely, with each counterpart classified (`peer_kind`, `peer_relationship`) —
meaning this plane also covers what the service says to its children and to the
outside, which the tunnel plane does not. But it is opt-in (`include_packets`) and it
degrades to `conntrack`, and then only counters exist. The receipt carries
`capture_mode` and `degraded_reason` inside the signed payload; a conntrack receipt
attests volumes and must not read as more.

**A direct connection to the instance's published port.** The node is not on the
application path at all; only the tap sees anything.

**End-to-end TLS through the tunnel.** The node signs a digest of ciphertext. This is
still worth having — the client holds the session keys and can substantiate the
plaintext later — but the receipt says so, because a digest of an opaque blob and a
digest of the exchanged messages are not the same claim.

> **Consequence:** a receipt covering only the client's tunnel does not establish that
> the service spoke to nothing else. Whoever reads it will ask, so the coverage field
> answers it: *client tunnel*, *full instance tap*, or *counters only*.

---

## Shape: a hash chain with signed checkpoints

A single signature at the end of an execution is worthless. The node simply declines
to sign whenever the outcome is inconvenient, and the client is left with the same
nothing it started with. The receipt therefore accrues:

- a chain over events, each contributing `(seq, direction, length, digest(payload))`,
  so reordering or dropping a record breaks it;
- a **signed checkpoint** every so many bytes or seconds, carrying a monotonic `seq`
  and the cumulative counters, plus one at close;
- in a dispute, the highest `seq` either party can produce is the truth of the matter.

This is the same argument that already governs billing in `TrafficMeter`: a tunnel has
no fixed length, so charging only at the end would let a caller relay for free by
never closing. Attesting only at the end fails in the mirror-image way. With periodic
checkpoints, refusing to sign stops being free — the client holds checkpoint *k*, and
the node can prove nothing about anything after it.

Cost is dominated by hashing, which is linear in bytes already being copied; Blake2b
keeps one hash family across the identity path. An Ed25519 signature is tens of
microseconds and is paid per checkpoint, not per byte.

Mechanically, `ServiceTunnel` already multiplexes `{1: TokenMessage, 0: bytes}`, so
checkpoints fit as a third index without disturbing the byte pipe, and `Observe` can
emit them as one more `kind`.

---

## What is actually decidable from a receipt

This is the part that decides whether receipts are worth building, because a receipt
saying "I ran S with E and relayed these bytes" is not by itself an accusation. What
makes it one, ordered by how little the reader has to take on faith:

**1. Two receipts that contradict each other.** Same execution name, incompatible
content, both signed by the same key. No interpretation is required and no third party
is needed: the node has published a proof that it lies. This is the strongest outcome
available, and it costs nothing to support — it falls out of signing at all.

**2. A receipt against the node's own advertisement.** A `Peer` announcement is signed
and covers `mu_per_call` and the payment contracts (`canonical_peer_content_digest`
exists precisely so those cannot be tampered with). A receipt showing what was charged
against what was relayed, read next to the rates the node advertised, makes
overcharging decidable from two signed objects and nothing else. Given that
`TrafficMeter` already knows both the byte count and the MU billed, this is the most
valuable case reachable today, and it is also the one that matches the node's own
accounting rule: errors are supposed to land against the node and be self-correcting,
which only holds if something can review them.

**3. A receipt against the service's manifest.** `service_id` is content-addressed, so
the manifest — API, declared resources, cost model — is public and immutable. A
receipt whose envs or resources are inconsistent with it, or whose final checkpoint
falls far short of what was paid for, contradicts a document neither party controls.

**4. A missing or truncated receipt.** The client paid, holds checkpoints up to *k*,
and has no final one. This is a judgement, not a derivation: it rests on a policy that
a node which took payment must be able to produce a closing receipt. It is the only
category where the client's word matters, and therefore the one a reader should
discount.

**5. A reproducibility contradiction.** Re-run `service_id` with the signed envs and
the signed input transcript; if the output digest differs, the receipt is false.
Strongest in principle, available only for services that are deterministic, which most
will not be. Sources of divergence — time, randomness, external hosts, concurrency —
have to be recorded or declared away, so treat this as something a service opts into
rather than a general mechanism.

Note what categories 1 to 3 have in common: **they need nothing from the client**. The
absence of a client key costs them nothing, because the accused signed the evidence
and the counter-document is either his own other signature, his own advertisement, or
a content-addressed manifest. That is why a unilateral receipt is still worth having.

---

## Publishing a receipt

Registers are scarce — the on-chain `Peer` object drops every component's `prose`
because a kilobyte is the entire budget a box pays rent on forever. So what goes
on-chain is a **digest**; the receipt body travels off-chain, content-addressed, and
the digest proves which body was meant.

The reputation box already has the shape needed (`contracts/ergo/transaction.py`):
`R4` names the type of object an opinion is about, `R5` the object itself, `R8` carries
the sign of the amount — so a **negative** opinion is already expressible — and `R9`
carries content.

Two ways to model the complaint:

**(a) A negative opinion about the node, with the receipt digest in `R9`.** One box.
`R5` is the accused `peer_id`, the weight is negative, `R9` points at the evidence.

**(b) The receipt as an object type of its own.** `R4` is a receipt-type NFT, `R5` the
receipt digest; the negative opinion about the node is a separate box that cites it.

**(b) is the right target.** An opinion is subjective by construction and its weight
is the author's stake; a receipt is a fact signed by the accused. Collapsing the two
into one box makes the fact inherit the author's subjectivity — a reader who need not
trust anybody would have to weigh who published it. Kept separate, the receipt stands
on its own and the stake sits where stake belongs, and several complainants can cite
the *same* receipt, which is the one aggregate here that means something. (a) is an
acceptable stopgap only because `R9` keeps the fact separable from the opinion around
it.

Two practical notes. Publishing costs a box plus a fee, which prices spam without any
extra rule. And only a client with a wallet can publish at all — an external caller
without one hands the receipt to somebody who does. That works because a receipt names
no client: **it is transferable evidence**. Which is also the risk — it is not
revocable and can be republished forever, so what it exposes matters.

## Privacy

A receipt commits, it does not disclose. Envs can hold secrets and a transcript is the
client's data, so the signed payload carries digests: `blake2b(salt || canonical
envs)` with the salt in the body the client holds, and a Merkle root over transcript
chunks so a tranche can be substantiated without publishing the rest.

That leaves a tension worth naming: a complaint nobody can check is not a complaint,
so at some point the body has to be revealed to whoever judges. The resolution is
symmetric, and in the node's favour for once — **the node holds the same body**. A
client that publishes a digest and refuses to substantiate it is publishing noise, and
the node can call the bluff by revealing the body itself, against a digest that is
already public and that it signed.

## Delegation

When execution is delegated (`delegate_execution`, the `delegated_instances` table),
the node that took the request cannot attest the execution. It can sign that it
delegated, and pass on the executor's own receipt: a client that asked A and was
served by B ends up with a chain A→B. A cannot forge B's receipt — the signature is
B's — but it can **omit** it, so the executor's receipt is something the client
requires as a condition, not something it hopes to receive.

## What receipts do not give

- **No proof that the output was correct** for the input. This is not verifiable
  computing, and no signature can make it so.
- **No proof of time.** Timestamps are the node's own. If time matters, include a
  recent block hash (which bounds the receipt from below) and publish the final digest
  (which bounds it from above); only digests go on-chain, so the cost is trivial.
- **No coverage the node did not have.** See the four planes.
- **No positive reputation.** By construction; see
  [Unilateral by decision](#unilateral-by-decision).

## Keys and conventions

Receipts are signed with the identity key itself, not a subkey. A key derived from the
same mnemonic under a different personalisation is possible — `node_identity` is
written to allow exactly that — but it would need a further attestation to be tied back
to the `peer_id`, and losing the direct verification against the `peer_id` is losing
the point. The marginal exposure is small: that key already signs `GetPeerInfo`, an
unauthenticated RPC anyone can call.

Non-negotiable, all for reasons already argued in the identity module:

- **A domain prefix of its own** (`celaut-execution-receipt:`, beside
  `_ATTESTATION_PREFIX`). Signing a receipt with a bare payload would let it be
  replayed as some other thing this key signs, and the other way round.
- **Canonical encoding field by field**, never `SerializeToString()`.
- **Blake2b-256**, one hash family across the identity path.
- **The signature scheme is declared**, as it is on the `Peer`: which cryptography a
  signature is in decides how it is verified, so it is part of what is signed.

---

## Implementation path

1. **A start receipt.** Sign `(peer_id, execution name, service_id, envs digest, ts,
   initial MU)` and return it from `StartService`. Every field already exists in
   `local_instances`; no data path is touched.
2. **Chain and checkpoints in the tunnel.** Hash alongside `TrafficMeter.add`; emit
   checkpoints on a third `ServiceTunnel` index.
3. **A closing receipt with declared coverage**, and checkpoints over `Observe` with
   `capture_mode` inside the signed payload.
4. **Publication**: the receipt-type object, the digest on-chain, the body
   content-addressed off-chain.

Phases 1 and 2 are independently useful: phase 1 already makes categories 2 and 3
above reachable for anything billed per call, and phase 2 is what makes an
interrupted execution provable.

## What a client identity would add

Deferred, and recorded here so the shape is known when it arrives. Given clients that
present a key of their own:

- **The node could prove what the client agreed to** — envs, price, request — which is
  what turns a receipt from evidence against the node into a record binding both.
- **Omission would become visible.** With a per-client sequence the client
  countersigns, gaps in a published set are detectable, so selective publication stops
  being free.
- **Positive reputation would become possible.** Receipts signed by a counterparty the
  node did not invent can be counted; receipts a node issues to its own namespace never
  can.

Until then, a receipt is exactly one thing: something a client can hold up, that the
node cannot deny.
