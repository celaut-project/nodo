# `Service.Network` — Logical Communication Domain

A `Network` defines **which peers a service wants to connect to** and **which protocols those peers must expose**.

## Message Specification

```proto
message Network {
    // Descriptive tags for classification and discovery
    repeated string tags = 1;

    // Human-readable description of the communication domain
    string prose = 2;

    // Formal/machine-readable description of the domain
    // (e.g. a canonical expression)
    bytes formal = 3;

    // Protocols that peers in this network must support
    repeated Api.Protocol protocol_stack = 4;

    // Environment variable used to filter compatible peers during
    // network resolution. Only peers whose value matches that of
    // the requester are returned. Empty = no filtering.
    string environment_variable = 5;
}
```

---

## Peer Filtering by Environment Variable

A network may contain multiple instances of the same service, but a client typically needs only those that share a particular property.

**Example:** Many PostgreSQL instances may exist, but a client only needs those belonging to its own cluster. By setting `environment_variable = "PG_CLUSTER"`, network resolution returns only the peers whose `PG_CLUSTER` matches the requester's value.

* **Implementation:** `src/manager/network_env.py`
* **Consumer:** `resolve_network()`

---

## Network Instance Indexing

A node indexes an instance as a member of a network only if **both** of the following conditions are satisfied:

| Condition                                            | Meaning                                                                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **1. Declares the network in `Service.Network`**     | The service *wants* to connect to peers in that network.                                                                             |
| **2. Exposes the `protocol_stack` in `Service.Api`** | The service *can* be consumed by others. If it does not expose the required protocols, it is treated as a consumer-only participant. |

---

## Authorization: the Ancestor Chain

Declaring a `Network` is a request, not a grant. An instance launched by another
local instance may only use the networks that **every** generation above it also
declares: the requested set is intersected, by tag match, with the direct
father's spec, then with its father's, up to the topmost local ancestor.

* **Implementation:** `filter_networks_with_ancestors()` in `src/manager/networks.py`
* **Consumer:** the virtualizer, while building `ConfigurationFile.NetworkResolution`

The rule is "only the direct father authorizes" applied by induction: a father can
only pass on the domain its own father passed to it, so "father yes, grandfather
no" is not a reachable state. The walk re-derives that grant at launch time from
each ancestor's *spec* — what it asked for — because the node does not persist
what each instance was effectively granted.

Because the grant is re-derived rather than stored, it depends on the ancestors'
specs being readable from the local registry. When one is not — missing, or not
loadable at that moment — nothing is granted and the launch is aborted; a spec
this node cannot read is never read as a spec that declared no restrictions. A
launch that requests no network needs no ancestor spec at all.

---

## Operator Policy: `service_networks`

The ancestor chain answers "did the instances above this one ask for the same
domain?". It never answers "does the person running this node want it reached at
all". That is a separate control, in `config.yaml`:

```yaml
service_networks:
  blacklist:
    - "*google.com"
  whitelist:
    - "dns:*"
    - "pow:bitcoin"
```

* **Implementation:** `src/utils/network_policy.py`
* **Consumers:** `launch_service()`, `GetServiceEstimatedCostIterable`,
  `_build_network_resolution()`

Both lists empty — the shipped default — restricts nothing.

### Rules

| Rule | Meaning |
|---|---|
| Blacklist first | It is evaluated over every tag before the whitelist is, so a tag on both lists is rejected and reported as blacklisted. |
| Glob, case-insensitive | `fnmatch` over the tag, lowercased on both sides. Glob over the *tag* and nothing else: `google.com` does not match `www.google.com` — write `*google.com`. |
| Every tag must pass | A non-empty whitelist has to cover each tag of each declared network. A network is not one destination, it is as many as it names: `resolve_network` walks the tags one by one and stops at the first that resolves, and the firewall reads them one by one too. A tag nobody vetted is a destination nobody vetted. |
| No network, no question | A service that declares none is always accepted; it asked for no domain. Same for a network with no tags, and for an empty tag: they name nothing, the resolver ignores them and the firewall opens nothing for them. |
| `blacklist: ["*"]` | Refuses every service that declares any tagged network — "nothing beyond this node". |

The block is `service_networks`, not `networks`: `network:` is this node's own
ports and addresses, and a typo between two names one letter apart would leave the
node with no policy while looking configured. A `networks:` block carrying
`blacklist`/`whitelist` keys is therefore reported as a config error rather than
ignored.

### Where it is enforced

| Point | Judges | Why there |
|---|---|---|
| `launch_service()`, before the balancer | What the service **declares** | Before the balancer, so it covers delegation: a node that refuses to reach a domain itself and then pays a peer to reach it has outsourced a policy, not applied one. Before the `force_execution` bypass too, which overrides peer *selection* and not what this node will have reached on its behalf. |
| `GetServiceEstimatedCostIterable` | What the service **declares** | A price is an offer. Quoting a service this node would refuse only gets the asking peer's balancer to select it and fail at launch. |
| `_build_network_resolution()` | What **survived** the ancestor chain | Defence in depth, on the narrower set that is actually about to be opened. It aborts the launch rather than dropping the network: reaching it means an earlier check did not run, and a guest silently started without the egress it asked for is exactly the unexplained rejection this policy replaces. |

The declaration is what the first two judge, and it is what the client can see and
change; the launch is refused even when the ancestor chain would have dropped the
offending network anyway.

`nodo serve` reads the policy once at start and logs it, restrictions or none — a
control nobody can see in the log is one nobody can tell is in force. A policy the
node cannot parse stops it there, rather than failing every launch later; a list
this node failed to read is never read as a list that allowed everything.

### What the client is told

```
Network policy: this node refuses to run a service for service <hash> that reaches 'maps.google.com'.
  declared networks:
    #1: maps.google.com, dns:google
    #2: pow:bitcoin
  rejected tag:      maps.google.com (network #1)
  rule:              service_networks.blacklist
  pattern:           *google.com
```

A whitelist miss replaces the `pattern:` line with `matched none of:` and the
whitelist. Either way the report names every declared network and not just the
offending one, because the client sent a set and a verdict on one tag of it says
nothing about the rest.

### Scope

Every service, including the core services the node starts for itself (packer,
source-application, low-demand-fallback). Their egress is egress from this node
too, and exempting them would make `blacklist: ["*"]` a claim the node does not
keep. An operator who needs one of them whitelists what it needs.

This is a policy on what a service may **ask** to reach, not a guarantee about
what it can reach. The firewall is what confines a running guest
([`FIREWALL.md`](FIREWALL.md)); this decides whether the guest starts at all.

---

## Use Cases

### 1. DNS

> "Give me access to `www.google.com`."

A simple DNS network where peers resolve domain names.

---

### 2. TLS Notary (Third-Party Attestation)

Scenario: a service needs to access third-party resources (e.g. OpenAI) but does not want to manage its own credentials or blindly trust remote peers.

**How it works:**

* An operator (Alice) with an OpenAI subscription runs a proxy service.
* Alice's service exposes the network with `protocol_stack = "tlsnotary over https"`.
* Other services (Bob) can discover and consume Alice's instance, paying per use.
* TLS Notary provides cryptographic proofs (zk-proofs) that the response is authentic.

**Concrete example:**

> **Alice** has an unlimited OpenAI subscription. She runs a service with:
>
> * `Network.formal` ≈ `"openai.com/api/v1/completions model=gpt-5.6 tls-notary"`
> * `Network.protocol_stack` = `"tlsnotary over https"`
> * `Api` = `"openai.com/api/v1 http port 8080, cost: 0.001 BTC / 1M tokens"`
> * `Container.Env` = *OAuth tokens for her subscription* (never leave the VM)
>
> **Bob** wants to use GPT-5.6. His service declares the same network. His node discovers Alice's instance, and Bob can consume it on a pay-per-use basis—without needing his own API key or placing implicit trust in Alice.

---

### 3. Blockchain PoW

> "Give me peers in the PoW network with architecture X, latest block ≥ B, and difficulty ≥ D."

The formal description (`formal`) encodes the consensus requirements used to select
peers. This is the first network kind where `formal` carries the ask: the tag
(`pow:ergo`, `pow:bitcoin`) names the *chain*, and two services on the same chain
asking for different blocks or different work are not in the same domain.

| | |
|---|---|
| Tag | `pow:<chain>` — `pow:ergo`, `pow:bitcoin` |
| `formal` | The `key=value` body every celaut descriptor uses (`node_identity.component_formal`): sorted lines, UTF-8. This domain's keys are namespaced `pow.` — `pow.chain`, `pow.block_id`, `pow.min_cumulative_difficulty`, optional `pow.min_height` and `pow.max_tip_age_s`. A key outside that vocabulary is **preserved and round-tripped, not refused** and not enforced; a missing required key or a malformed value still is refused. No version key: a version belongs to the vocabulary, which `protocol_stack` names. |
| Not in `formal` | Which protocols the peers speak. `protocol` and `peerDiscovery` are tags/prose/formal descriptors in their own right, which is what `Service.Network.protocol_stack` (`repeated Api.Protocol`) already models — flattening them into a key here would be a second place for the same thing to be stated, and to disagree. |
| Difficulty | **cumulative work since genesis** (Ergo `fullBlocksScore`, Bitcoin `chainwork`), not the tip block's difficulty: it is what the chain's own fork choice maximises, it is monotone, and it gives a total order peers can be compared on. Carried as a decimal string — the value outgrew a double long ago. |
| Containment | the block must be on the peer's **main** chain (`/blocks/{id}/header` then `/blocks/at/{height}`), not merely stored: an orphan a peer kept is not a block its chain contains. |
| Ancestors | `match_networks` compares `formal` when both sides declare one (`node_identity.same_component`, the rule every tags/prose/formal descriptor is compared by), and falls back to a shared tag. So a parent granting a specific ask grants **that** ask; a parent meaning "any `pow:ergo`" leaves its own `formal` empty. |
| Endpoints | A `pow:` domain has no name to look up, so its addresses are *found*, from four sources in trust order: `ledgers.ergo.NODE_URL`; `service_networks.default_instances["pow:ergo"]` (named by hand); other nodes over `Gateway.ResolveNetwork`; and the crawl at `ledgers.ergo.HTTP_PEERS_PATH`. Every one is verified identically, so the order decides only who is asked first. |
| Checked over REST, handed over **P2P** | A candidate is *verified* at its `restApiUrl` — only the REST API can answer "does your main chain contain B". What is then emitted is that peer's **P2P** endpoint, because a service declaring `pow:ergo` is asking for chain peers and no chain protocol is spoken on the REST port. The REST uri is **not** also emitted: an `Instance.Uri` carries an ip and a port and no role, so a second uri would be indistinguishable to the guest while the firewall opened both — a hole that is at once useless for the ask and open. A guest wanting a REST API asks for one, and that is a different network descriptor. |
| Where the P2P port comes from | **Observed, not assumed, wherever anything observed it.** The crawl keeps each peer's `/peers/connected` `address` (`p2pAddress`), so a peer on 9031 is reached on 9031 — of 58 peers on one live mainnet node, five were not on the conventional port. A candidate whose P2P endpoint nobody ever saw (`NODE_URL`, `default_instances`, a peer's `ResolveNetwork` answer, or a crawl entry written before this field existed) reuses the REST **host** with `pow_networks.ERGO_P2P_PORT` (default 9030), and every such assumption is logged. Reusing the host is not an assumption of the same kind — that is where the node that answered `/info` lives; only the port is a guess. Ergo's `/info` does not report the node's own P2P address, so there is no way to ask a candidate for it directly. |
| Shape | **one `Instance` per endpoint**, not one with N uris: they are separate operators, separately verified and separately reachable. One Instance with several uris means "one peer at several addresses", which is what a DNS name's A records are. |
| No peer qualifies | resolves to `[]`, like any other unresolved tag — "nobody meets D right now" is transient and about the world, not about the request. |
| Config | `pow_networks.TIMEOUT_SECONDS`, `.MAX_PEERS`, `.ASK_PEERS`, `.ERGO_P2P_PORT`; `service_networks.default_instances` |
| Firewall | `configure_guest_firewall_policy` opens exactly the uris of each peer instance — i.e. the P2P endpoints above, one rule per peer. Nothing downstream re-derives a port: the resolver put a concrete one in every uri, which is the point of doing it there. |
| Implementation | `src/manager/pow_networks.py`, dispatched from `resolve_network()`; the P2P address is carried through the crawl by `src/manager/ergo.py` |

> ⚠️ **What is verified is what the candidate says about itself.** Its REST answers
> are claims, and a peer can fabricate all of them cheaply. That excludes the common
> failure — an out-of-sync, stalled, pruned or wrong-network node — and not a
> deliberate liar. Cross-checking *k* of *n* candidates, and verifying the Autolykos
> solutions in the headers themselves, are the next two steps.

> ⚠️ **What a suggested endpoint buys, and what it does not.** Another node naming an
> address grants nothing: what it buys is a place in the queue. Every address is
> verified the same way as one the operator typed into `config.yaml`, so the cost of a
> lie at that layer is a wasted HTTP request, not a firewall rule.

> **Reading endpoint lists off the reputation ledger is deliberately out of scope
> here.** How a communication domain is formalized on-chain is still being settled
> with [`celaut-project/skills`](https://github.com/celaut-project/skills/issues/72),
> and wiring a reader against a schema that is about to change would bake in the
> version we are least sure of.

Bitcoin parses and does not resolve: nodo's default Bitcoin posture is a
receive-only Esplora backend, which exposes neither `chainwork` nor a peer list.

#### Declaring it in `service.json`

The packer writes `formal` and `protocol_stack` from the network entry. `formal` is a
flat object of **string** key/value pairs, encoded to the sorted `key=value` body by
`node_identity.component_formal` — so authoring order never changes the packed bytes,
which matters because this field is compared byte for byte.

```json
{
    "network": [
        {
            "tags": ["pow:ergo"],
            "prose": "An Ergo node whose main chain contains this block",
            "formal": {
                "pow.chain": "ergo",
                "pow.block_id": "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b",
                "pow.min_cumulative_difficulty": "1152921504606846976",
                "pow.max_tip_age_s": "3600"
            },
            "protocol_stack": [
                {"tags": ["ergo-node-api"], "formal": {"api.version": "4"}}
            ]
        }
    ]
}
```

Values are quoted because a `formal` body is text: `pow.min_cumulative_difficulty`
passed 2\*\*64 long ago, and an unquoted JSON number would be an IEEE double that
rounds the requirement away before it is ever packed. The packer refuses one rather
than converting it.

The ask is run through `parse_pow_formal` **at pack time**, so a missing
`pow.block_id`, a non-integer difficulty, or a `pow:ergo` tag whose body says
`pow.chain=bitcoin` fails the pack instead of the launch of an already-published
service. Nothing contacts a peer while packing. Keys outside the `pow.` vocabulary
are preserved and packed, not refused — the packer is no more the ceiling on what a
domain may say than a resolver is. A `pow:` tag with **no** `formal` is left alone:
that is the "any peer on this chain" an ancestor declares when it grants the whole
chain rather than one instance of it.

Syntax reference: [`PACKING.md` → `network`](PACKING.md#network).

### Asking another node

`Gateway.ResolveNetwork` takes **any** `Service.Network` and answers with the peers
this node knows in that domain. Generic, not `pow:`-shaped: a domain is declared the
same way whatever resolves it, and a caller that had to know in advance which kind it
held would be doing the resolving itself.

The operator's `service_networks` policy applies to it — resolving a domain this node
refuses to reach is reaching it by proxy — and a node **never relays** the question
(`resolve_network(..., ask_peers=False)`). Two nodes that know each other are a cycle
of length two, so relaying would turn one request into a flood over a graph nobody has
a view of. Each node answers from what it knows locally; a caller that wants more
breadth asks more nodes itself.

Full design, and an audit of what the DNS path guarantees today:
[`proposals/78-network-guarantees-and-pow.md`](proposals/78-network-guarantees-and-pow.md)
(issue [#78](https://github.com/celaut-project/nodo/issues/78)).

---

### 4. Generic Networks

> "Give me instances of the network with architecture X."

Applicable to any kind of network:

* **PoS:** Proof-of-Stake networks
* **P2P:** Arbitrary peer-to-peer networks


### Operator default instances (all network tags)

`service_networks.default_instances` maps any exact network tag to URI seeds.
For example `"my-domain": ["tcp://192.168.1.60:1234"]`. Valid seeds take
precedence over DNS for ordinary domains; invalid/unresolvable seeds fall back to
the existing resolver. Each resolved address is a separate Instance. PoW domains
consume the same map through their candidate pipeline and still verify every peer.
Move existing `pow_networks.ENDPOINTS` entries here; the old setting is retired.
Generic peer discovery and reputation readers remain reusable helpers, but their
untrusted suggestions are only wired to the PoW verifier, not blindly granted as
members of arbitrary domains. Operator defaults are explicit operator assertions.
