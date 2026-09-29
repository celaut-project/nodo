# Peer gossip: bounded transitive discovery (#427)

A node seeded with B can learn C from B without the operator connecting A to C.
`Gateway.ListPeers` streams existing `Peer` messages in the usual bee-rpc `Buffer`
envelope; `IntroducePeer` carries proactive announcements. Neither RPC relays a
query or triggers an immediate onward push. The manager calls independent,
self-gated pull and push ticks, each selecting one random known peer every 300s.

## Privacy and signatures: an explicit adjustment to the issue plan

The issue proposed removing non-global URIs while also relaying the original signed
advertisement. Those requirements cannot both be met: `canonical_peer_content_digest`
covers all URIs and their expiry. A filtered claim fails verification, and removing
its signature/public key makes registration refuse it. Re-signing under the relayer's
key would misattribute somebody else's addresses and contracts.

Therefore both relay paths share one **whole-advertisement eligibility rule**:

- Read `peer.advertisement`, not merged address-table rows.
- Require the advertised public key to match the database row and a signature to be
  present. Mismatched identities are skipped, never downgraded to unsigned claims.
- Every URI must be a global unicast IP literal. Never relay a third party's private,
  loopback, link-local, multicast, reserved, or otherwise non-global address, even an
  expired one. This is unconditional; `ANNOUNCE_PRIVATE_ADDRESSES` only decides what
  this node announces about itself.
- Do not assume arbitrary DNS names are public. Split-horizon/local names and DNS
  rebinding make that unverifiable without changing the protocol; withhold DNS-bearing
  claims too. Self-announcement keeps its existing DNS support. Direct connection
  remains available for DNS-only, LAN-only and mixed public/private peers.
- Require at least one unexpired URI (`expiry=0` means no declared expiry). Keep all
  public URIs, including expired ones, in the signed object; editing them breaks it.
- Send the original fields/signature unchanged. Receivers still do full cryptographic
  verification and monotonic timestamp checks through `add_peer_instance`.

A future selective-disclosure format or separate subject-signed public-only claim
could allow mixed-address nodes to gossip their public address. It is not safe to
approximate that by mutating today's claim. `lan-lab` enables gossip as requested,
but LAN-only announcements intentionally do not travel transitively.

## Wire and authentication

Current upstream implemented #428 after the issue plan: every RPC except
`GenerateClient` requires a client id (local guest exemption unchanged). `ListPeers`
therefore accepts a `Client`, not an unauthenticated empty request, parses it with the
normal simple-RPC read deadline, and calls `require_caller`. Pull/push obtain a client
id through the existing peer-client machinery; push attaches it to `IntroducePeer`.
The shared per-client rate limiter applies. Sharing off returns an empty response
for an admitted caller; it does not bypass authentication.

The four gRPC Python stub/servicer/registration/static-helper sites are hand-edited,
as for `ResolveNetwork`. No new message type or descriptor-based reflection is used.
`BeeClient.list_peers` centralizes the client wire shape; all response framing remains
in BeeClient. Unknown methods on older nodes are contained failures, not a fatal
manager error.

## Limits and runtime behavior

See [CONFIG](../CONFIG.md#transitive-peer-discovery-gossip) for all five live config
keys and CELL profile defaults. List responses cap at 100 eligible advertisements.
Pull also caps at 100 messages locally, counting malformed/rejected candidates
against the cap, and closes the stream/channel when finished. One pull RPC has a
10s deadline. Push caps at 20 advertisements, excludes its target, and has one shared
10s RPC budget rather than twenty sequential ten-second waits. Normal peer transport
and client acquisition precede these RPC budgets. No extra threads or durable
per-target state are introduced.

Ticks have separate monotonic clocks, record attempts even on failures, and contain
both per-item and outer failures. Bad rows, a refused introduction, failed registration,
or an interrupted stream do not kill maintenance. Caps of zero disable work; invalid
numeric values fall back to defaults. Connection resources close on success or error.
Cancellation stops list serialization. Serving a list is local-only database work.

## Anti-replay and known costs

A malicious relay can replay a real signed `(peer_id, ts)` it has observed, but cannot
forge a higher timestamp or alter an address/payment contract. Normal anti-replay
checks prevent downgrades. An already stale but not superseded address can remain
pinned until a fresher signed claim arrives by direct connection, another relay, or
expiry-triggered refresh. Pull logs the source relay and subject separately for
traceability, and must never use `accept_peer_refresh` (which would require C's claim
to have B's identity).

Repeated pushes to mutual peers are bounded steady-state waste, not unbounded
amplification. Candidates are walked in a fresh random order on every list and push,
so a table larger than the cap still propagates in full over successive ticks rather
than relaying the same first 100/20 forever. A per-(target, subject, timestamp) sent
cache is a possible future optimization; v1 bounds sends without adding state.

A node never registers its own identity. Once B learns A, B's list contains A's own
advertisement; `add_peer_instance` refuses a claim carrying this node's key (so an
`IntroducePeer` of it is answered `REFUSED`), and pull skips it before verifying.

## Gossip does not choose whom to pay

Minting an Ed25519 keypair, a public address and a payment contract is free. With
`network.DELEGATE_EXECUTION` and `deposits.AUTOMATIC_REFILL` both on (`open-renter`),
`maintain.peer_deposits` sends a full deposit to every reachable peer below its refill
threshold. That was already reachable by direct `IntroducePeer`, but gossip turns one
Sybil introduction into a network-wide one: every honest node relays it onward, and
every refill-enabled node that learns it pays it.

So provenance is recorded. `peer.learned_via_gossip` (an `INTEGER NOT NULL DEFAULT 0`
column, added by the usual `ensure_columns` migration; existing rows are 0) is set when
a peer is *first* registered through gossip pull, or through `IntroducePeer` from a
caller whose client id is associated with a different known peer, i.e. a push relay.
It never demotes a peer already known. The automatic refill skips a flagged peer. The
flag clears when the operator dials it (`nodo connect`) or any deposit to it settles
(`nodo pay`, `nodo increase_peer_deposit`), so a deliberately chosen peer is funded as
before.

Tradeoffs. An unassociated `IntroducePeer` caller cannot be told apart from a
self-announcement and is treated as one; that is the pre-existing direct path and is
not made worse. A flagged peer is still refreshed, listed and relayed; it is only
unfunded, so delegating to it needs a deposit made by hand first. Being *used* for
delegation does not by itself clear the flag, because delegation to a peer already
depends on a deposit. A per-peer spend cap or reputation-weighted refill would be a
broader policy change and is left out of this PR.
Caps bound transmitted/consumed peer counts, not database scanning or arbitrary
protobuf message size; the existing transport and receiver validation remain relevant.

## Verification

`tests/test_gossip.py` covers disclosure and signature preservation, row mismatch and
expiry, independent live flags/clocks, caps (including hostile replies), per-item
failures, client-id attachment, total push deadline, channel cleanup, four-way RPC
wiring, and a real localhost gRPC stream through the actual Gateway handler. A real
SQLite/signature test checks A learning C from B, refusal of a forged C, and retention
of a newer C announcement against replay, that A never registers its own relayed
advertisement, and that a gossip-learned peer is not auto-funded while a connected one
is. CELL tests pin all five profile postures.

Full daemon smoke recipe: seed A↔B and B↔C only, lower the interval for the test, and
watch A register C when C announces only public addresses. Repeat with C announcing
both public and private addresses: A must not learn C through gossip at all. Restore
the interval after testing. This requires appropriately reachable peers; do not use
private addresses to claim the public-only case passed.
