# Issue #78 follow-up review (2026-09-16)

Reviewed against PR #366 head `181b7f2f` and Josemi's
[request](https://github.com/celaut-project/nodo/issues/78#issuecomment-5694918928).

## Requested changes

- `NetworkFormal` is a protobuf `map<string, bytes>` serialized into the existing
  `Service.Network.formal` bytes field. Known PoW fields remain strictly validated;
  unknown entries survive as opaque bytes. They are not advertised as enforced
  requirements. New mandatory semantics require a supported version.
- `service_networks.default_instances` replaces `pow_networks.ENDPOINTS`, with
  exact tags mapping to URI lists. Non-PoW tags now use operator seeds before DNS;
  PoW uses the shared map without bypassing chain verification. Invalid seeds
  don't prevent fallback. Separate addresses yield separate Instances.
- Identity/signature formal encodings are unchanged. Old PoW text specs need
  repacking; old endpoint configuration needs moving to the new key.

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
3. **Reputation endpoint type:** the reader selects the configured type NFT in R4
   and exact descriptor digest in R5, checks the contract tree, reads polarity
   from R8 and URL lists from R9, and ranks by signed backing. Fixed duplicate URLs
   within a single box multiplying its influence. This is a reader, not a minted
   or deployed type NFT/publisher: the feature remains off until the operator
   supplies `NETWORK_ENDPOINTS_TYPE_NFT_ID`. Rankings are candidate ordering,
   never proof of chain state.

Generic untrusted peer/reputation suggestions are deliberately not granted as
members of arbitrary domains without a domain verifier. Operator default seeds
are explicit operator assertions, not peer attestations.

## Verification

Focused suite: **159 passed, 7 skipped, 5 subtests passed** across PoW parsing and
resolution, defaults, discovery, reputation endpoints, firewall hooks, ancestor
matching, component formal, network policy and policy enforcement. The seven
policy-enforcement cases skip for missing runtime dependencies in this environment.
Tests mock RPC/REST/explorer/firewall boundaries; no live chain or microVM E2E is claimed.

Full `pytest -q --disable-warnings` stops with the **same five collection errors**
on both `181b7f2f` and this update: missing assigned gateway port, installed bee-rpc
missing `block_pointer`, and the related manager-shares import failure. No host
firewall/config changes were made to work around these environment requirements.
