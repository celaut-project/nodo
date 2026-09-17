# Issue #78 follow-up review (2026-09-16)

Reviewed against PR #366 and Josemi's
[request](https://github.com/celaut-project/nodo/issues/78#issuecomment-5694918928),
then revised after the [follow-up](https://github.com/celaut-project/nodo/pull/366#issuecomment-5710933503)
(`3ca44749` onwards).

## Requested changes

- **`formal` stays the `key=value` body** celaut already documents for every other
  tags/prose/formal descriptor (`node_identity.component_formal`). `7bd12f76` tried a
  `NetworkFormal` protobuf `map<string, bytes>` and it was reverted: the extensibility
  the map was asked for does not come from the encoding, it comes from the *consumer*
  not refusing unknown keys, which is the change below. Sorted lines are canonical by
  construction where a map is explicitly not, they are the one convention this field
  has, and `celaut-project/skills` reads them with no protobuf dependency.
  `protos/network_formal.proto` and its gencode are deleted.
- **Unrecognized keys are preserved, not refused** (`_KNOWN_KEYS`' strict rejection is
  gone). They ride in `PowRequirement.extensions` as text and are re-emitted by
  `canonical_formal`; nothing enforces them. Missing required keys and malformed
  values are still refused — validating what is understood, not rejecting what is not.
- **No `v` key.** A version belongs to the vocabulary, which the sibling
  `protocol_stack` descriptor names; carrying it here too was versioning twice.
- **Domain keys are prefixed `pow.`**, and `protocol`/`peerDiscovery` are out of the
  body entirely: each is a tags/prose/formal descriptor of its own, which
  `Service.Network.protocol_stack` (`repeated Api.Protocol`) already models.
- `service_networks.default_instances` replaces `pow_networks.ENDPOINTS`, with
  exact tags mapping to URI lists. Non-PoW tags now use operator seeds before DNS;
  PoW uses the shared map without bypassing chain verification. Invalid seeds
  don't prevent fallback. Separate addresses yield separate Instances.
- Identity/signature formal encodings are unchanged — they were already this
  encoding, which is the point. Old unprefixed PoW specs need their keys prefixed
  (an unprefixed `chain=` is a *missing* `pow.chain`, never silently reinterpreted);
  old endpoint configuration needs moving to the new key.

## Review of the three existing additions

1. **Instance/firewall shape:** PoW returns one Instance per qualifying endpoint;
   the firewall loops over all returned instances, not only the first. Covered by
   resolver and firewall-hook tests. This does not prove one distinct operator per
   endpoint, and individual failed firewall applications are still logged rather
   than causing rollback of the whole policy.
2. **Peer discovery:** `Gateway.ResolveNetwork` is wired through stub, handler and
   registration; operator policy is checked before answering; `ask_peers=False`
   prevents recursive relay. The helper is network-generic, but its untrusted
   results are currently consumed only by the PoW verifier. Added the missing
   10-second RPC deadline so an unresponsive peer cannot hang launch indefinitely.
   The peer response loses HTTP-vs-HTTPS scheme information when reduced to
   IP/port candidates; HTTPS-only suggestions remain a documented limitation.
3. **Reputation endpoint type: dropped from this PR.** The reader keyed R5 on a
   `blake2b(sorted tags ‖ formal)` digest of the domain, which does not match how
   `celaut-project/skills` identifies a network: there R5 is either empty (a
   self-contained Strict Definition) or an existing box id, and the descriptor lives
   in R9. An exact content digest is also not enumerable — it finds only boxes
   published for one byte-identical ask. Rather than ship a reader against a schema
   under revision, the source is removed and the alignment tracked in
   [`skills#72`](https://github.com/celaut-project/skills/issues/72).

Generic untrusted peer/reputation suggestions are deliberately not granted as
members of arbitrary domains without a domain verifier. Operator default seeds
are explicit operator assertions, not peer attestations.

## Verification

Focused suite: **118 passed, 33 skipped** across PoW parsing and resolution,
defaults, discovery, firewall hooks, ancestor matching, component formal, network
policy and policy enforcement. The skips are cases needing runtime dependencies this
environment lacks. Tests mock RPC/REST/explorer/firewall boundaries; no live chain or
microVM E2E is claimed.

Full `pytest -q --disable-warnings --continue-on-collection-errors` gives the
**identical failure and collection-error set** before and after the `formal` revert
(56 failed, 14 collection errors, unchanged line for line), with passes going
1538 → 1542 for the four added parser cases. Those failures are environment ones —
missing assigned gateway port, a `bee_rpc` without `block_pointer`, no host firewall
— and no host firewall or config change was made to work around them.
