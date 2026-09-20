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

## What a `formal` says: identity keys and selection keys

A `formal` carries two kinds of key. The distinction is a **convention, not a schema
change** — nothing in `celaut.proto` marks a key as one or the other, and no reader
enforces the split. It is a way of deciding, per key, *who gets to write the value*.

| | **Identity keys** | **Selection keys** |
|---|---|---|
| Answer | *What is this protocol?* | *Which concrete instance of it?* |
| Written by | The service author, once, in `service.json` | Whoever instantiates the service |
| Example | `pow.chain=ergo`, `pow.consensus=autolykos-v2`, `ledger.model=extended-utxo` | `pow.block_id`, `pow.min_cumulative_difficulty`, `pow.min_height` |
| Changes between instantiations | Never | Routinely — mainnet vs. testnet vs. a local chain |

The motivating mistake (issue
[#385](https://github.com/celaut-project/nodo/issues/385)) was writing selection keys
as if they were identity keys. `celaut-basics/ergo-node` hardcoded a concrete
`pow.block_id` and `pow.min_cumulative_difficulty` in its own spec — values that say
nothing about what Ergo *is*, and everything about which Ergo chain state the author
happened to be looking at. Two things follow from getting this wrong, and they pull
in opposite directions: pin the values and the service is frozen to one chain state
forever; leave them out and the node resolves `pow:ergo` against any peer that calls
itself Ergo, including a low-difficulty parallel chain that is not the mainnet the
instantiator obviously meant.

`${VAR}` is the third option: say *that* a key is a selection key, without saying
what its value is.

---

## `${VAR}` templates in `formal`

A selection key may carry the placeholder `${VAR_NAME}` in place of a value:

```json
"formal": {
    "pow.chain": "ergo",
    "pow.consensus": "autolykos-v2",
    "pow.block_id": "${ERGO_BLOCK_ID}",
    "pow.min_cumulative_difficulty": "${ERGO_MIN_CUMULATIVE_DIFFICULTY}"
}
```

The value is filled in at **launch** from the launcher-provided
`Configuration.environment_variables` — the same map a service's own environment
comes from, so an instantiator sets one variable and both the guest and the node's
resolver see it.

* **Implementation:** `src/manager/network_templates.py` — one regex, one module,
  shared by the launch path, the packer and the gateway. Three copies of a grammar
  are three chances for them to disagree.

### Grammar

| Rule | |
|---|---|
| Syntax | `${NAME}` where NAME is `[A-Za-z_][A-Za-z0-9_]*` — the C identifier shape every environment variable already has. |
| Whole values only | **A value is either entirely a placeholder or contains none.** `"${MIN_DIFF}"` is a template; `"abc${X}"` is **refused at pack time**. |
| Surrounding whitespace | Ignored: `" ${X} "` is a placeholder. |
| Repetition | One variable may answer several keys. |
| `pow.chain` | **Never templatable.** It is the identity key the operator's `service_networks` policy vetted through the tag it must agree with, and a chain chosen after that policy ran is a chain nobody vetted. |

**Why partial substitution is refused.** Not aesthetics. A `formal` value is compared
byte for byte by `match_networks`, and the `ResolveNetwork` subset check has to
classify each key as *fixed by the author* or *left to the instantiator*. A
half-fixed value is neither, and "may the caller change the `abc` part?" has no
defensible answer. Concatenation is also the classic shape of an injection, and this
field's structure — newline-separated pairs — is exactly the kind forgeable by
gluing. A value whose answer contains a newline is rejected for the same reason: an
instantiator answering `MIN_DIFF` with `0\npow.block_id=deadbeef` would otherwise be
*adding a key* to somebody else's declaration, and the firewall would open whatever
that key resolved to.

### Per-network resolution policy: eager or deferred

Decided **per network**, by whether its `formal` carries a template at all. There is
deliberately no global on/off switch and no config knob: most declared networks have
enough information to resolve eagerly, and there is no reason to make those pay the
cost of deferral.

| Case | What the node does |
|---|---|
| No `${VAR}` anywhere in the `formal` | **Eager.** Resolved at launch exactly as before this existed — the bytes are not even re-encoded. |
| Every `${VAR}` answered by `config.environment_variables` | **Eager.** Substituted, then resolved normally, and the result is written into `__config__`. The common case, and it needs no gateway client in the guest. |
| Any `${VAR}` unanswered | **Deferred.** That network alone is skipped: `resolve_network()` is not called for it and it is **omitted** from the `NetworkResolution` list. Other declared networks are unaffected. Not an error, and logged once naming the missing variables. |

**A missing variable never aborts a launch.** The only condition under which a
declared network can refuse a launch is an operator `service_networks` policy
violation — and that is judged on the **declared**, pre-substitution network, before
any of this runs. Judging it after would let a service evade a blacklist by leaving a
variable unset: the network would have been dropped before the policy ever saw it.

**What a deferred network means for the guest.** It boots with a `__config__` short
the entry it declared, and with no firewall rule toward those peers — default-deny,
which is the correct posture toward a domain nobody has decided on yet. It is the
same position a declared network in which no peer qualified is already in;
`configure_guest_firewall_policy` walks the resolutions it is given, so fewer entries
is a shape the launch path already handles. The guest resolves it later over
`Gateway.ResolveNetwork`, by which time it may know what it wants.

**Deferred resolution is opt-in by construction.** A service author who wants to
guarantee the instantiator gets to pin the chain must declare **at least one
templated key**. A `formal` with no template at all is resolved eagerly against
whatever superset of matching peers the node can find — which may be broader than
any given instantiator wanted. That is the pre-existing behaviour, kept, because
changing it would force every service declaring any network to embed a gateway client
just to start.

* **Implementation:** `build_network_resolution()` in
  `src/virtualizers/microvm/rootfs.py`
* **Tests:** `tests/test_network_templates.py`,
  `tests/test_network_resolution_templates.py`

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

* **Implementation:** `local_network_instances()` in `src/manager/networks.py`
* **Consumer:** `resolve_network()`, for every non-`pow:` domain, at launch and over
  `Gateway.ResolveNetwork`

The members found this way are offered as `peer_instances` in the
`NetworkResolution` **alongside** whatever the operator's `default_instances` or a
DNS lookup of the tag produced: a guest declaring `postgres` reaches both the
`postgres` this node runs and the one written in `config.yaml`. Condition 1 is
judged by `match_networks` (formal when both sides carry one, a shared tag
otherwise); condition 2 by pairing the slot's `protocol_stack` with the network's
the way any protocol stack on this node is compared. A network that states no
`protocol_stack` is satisfied by any slot.

What is offered per member is only the slot(s) that satisfied condition 2, with
their published addresses -- the consumer writes a firewall rule per address it is
handed, and an unrelated admin port on the same instance is not opened for it. A
member with no published address (the launcher recorded none; the caller was
expected to tunnel) is not offered. Over `Gateway.ResolveNetwork` the calling
instance is never offered itself. `Network.environment_variable` filters members
by their recorded launch environment, the same way it filters every other peer.

`pow:` domains do not take members this way: there, membership is verified chain
state, and a local instance that wants to be found enters through the same
candidate list as every other endpoint.

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
  `rootfs.build_network_resolution()`

Both lists empty — the shipped default — restricts nothing.

### Rules

| Rule | Meaning |
|---|---|
| Blacklist first | It is evaluated over every tag before the whitelist is, so a tag on both lists is rejected and reported as blacklisted. |
| Glob, case-insensitive | `fnmatch` over the tag, lowercased on both sides. Glob over the *tag* and nothing else: `google.com` does not match `www.google.com` — write `*google.com`. |
| Every tag must pass | A non-empty whitelist has to cover each tag of each declared network. A network is one destination under every name it answers to: the tags of an entry are synonyms, `resolve_network` takes the first of them that resolves, and the firewall reads them one by one. Which name answers is not the operator's to pick, so each of them has to be one the operator would have allowed. A tag nobody vetted is a destination nobody vetted. |
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
| `rootfs.build_network_resolution()` | What **survived** the ancestor chain, as **declared** | Defence in depth, on the narrower set that is actually about to be opened. It aborts the launch rather than dropping the network: reaching it means an earlier check did not run, and a guest silently started without the egress it asked for is exactly the unexplained rejection this policy replaces. Judged *before* `${VAR}` substitution, so whether a launch is refused never depends on which variables happened to be set. |

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

### 1. Hostname tags

> "Give me access to `www.google.com`."

A tag that is lowercase and contains a `.` is resolved **on the node** to its IPv4
addresses (`resolve_domain`) and the guest is granted those addresses, one `Uri`
per address and port.

**A declaration does not choose a peer's port.** Every other peer this module
resolves is an instance running on some node, and *that* node assigned the port when
it published it — local members are offered on the ports their launcher recorded, the
operator's seeds on the ports the operator wrote down, a `pow:` peer on the P2P port
somebody observed it on. Which is why an `Instance.Uri` carries a port and a
`Service.Network` does not.

A hostname is the one exception, because there is no instance and no node that
published one — only a name this node looks up, so the port has nowhere else to come
from. What the entry may state is the **standard** port the service answering to that
name is expected to be on, written `port=<n>` in the entry's `formal` (#389), the same
`key=value` body every other parameter of an entry goes in — `${VAR}` selection keys
(#385) included, since the formal is substituted before the resolution runs.

Nothing is read out of a `protocol_stack`, and nothing out of a tag. A stack says
which protocols the peers speak; it is not where a port lives, and a tag is a
protocol's plain name and never a number — `https` does not mean 443 here, for the
reason `identity/transport_stack.py` gives: *"celaut has no conventions to fall back
on, the proto is where a thing is defined"*.

An entry that states no port opens 80 and 443, as a bare hostname tag always has. A
`port` that cannot be honoured is refused at pack time, and a node reading one from an
older pack grants **nothing** for that tag rather than falling back to 80 and 443:
that fallback would open two ports nobody asked for on the strength of a declaration
the node could not read — the same hole the `pow:` resolution refuses when it declines
to emit a peer's REST uri alongside its P2P one.

**What is granted is addresses, not name resolution.** nodo serves no DNS and
opens no port 53 toward anything (see the note in
`src/virtualizers/microvm/network.py`); the guest's default-deny covers UDP as
well as TCP. So:

| Program inside the guest | Works with a hostname tag? |
|---|---|
| Reads `network_resolution` from `__config__` and connects to the addresses (the way `ergo-node` consumes its peers) | **Yes** — this is what the tag is for. |
| Takes a URL and calls `getaddrinfo()` — `curl`, `yt-dlp`, any HTTP library, any TLS client that needs the name for SNI | **No.** It fails at the lookup, before it ever reaches the allowed address. The declaration looks correct and the service makes no request. |

A service in the second row today declares `["*"]` and narrows inside the image
(exact-host check after parsing, one program allowed to open sockets). The design
that would close the gap — a service that reads `network_resolution` and serves
DNS from it to its siblings, reachable on 53 through an ordinary peer allow — is the
one the `network.py` note points at, and does not exist yet. Until it does, do not
write a hostname tag for a program that takes names.

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
| Identity vs. selection | `pow.chain` is the only **identity** key this vocabulary defines; everything else it reads (`pow.block_id`, `pow.min_cumulative_difficulty`, `pow.min_height`, `pow.max_tip_age_s`) is **selection** and may be written `${VAR}`. A service describing the protocol itself carries its identity keys as extensions — `pow.consensus`, `ledger.model` and such — which this module preserves and does not interpret. `pow.chain` is never templatable: the tag it must agree with is what the operator's policy vetted. |
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
service. Nothing contacts a peer while packing. A `${VAR}` passes
(`parse_pow_formal(..., allow_templates=True)`) wherever a concrete hash or integer
would be required — the packer is validating what the author wrote, not asking anyone
anything. The resolver gets the **strict** default, so it can never be handed an
unfilled `${ERGO_BLOCK_ID}` and go looking for a peer that holds a block named after
a variable. Keys outside the `pow.` vocabulary
are preserved and packed, not refused — the packer is no more the ceiling on what a
domain may say than a resolver is. A `pow:` tag with **no** `formal` is left alone:
that is the "any peer on this chain" an ancestor declares when it grants the whole
chain rather than one instance of it.

Syntax reference: [`PACKING.md` → `network`](PACKING.md#network).

### Asking another node — `Gateway.ResolveNetwork`

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

#### The subset rule

This RPC is the other half of deferred resolution: a network skipped at launch
because a `${VAR}` was unanswered is completed here. That makes it a way to ask for a
network the service never fully declared, so **when the caller is one of this node's
own guests, the request must fit inside what its service declared.**

Without the check, a guest that declared `pow:ergo` pinned to block B could ask here
for `pow:ergo` pinned to nothing and be handed peers on any Ergo-shaped chain — the
same over-broad resolution templating exists to prevent, reached from the other
direction.

| The request | Verdict | Why |
|---|---|---|
| Same tags (as a **set**, order irrelevant) | Required | A tag is what the operator's policy vetted and what dispatches the resolution. A request naming a different tag is asking about a different domain, not narrowing this one. Set equality, not intersection: `match_networks` may accept one shared tag between two *declarations*, but this is a caller proposing a completion of its own. |
| Every key the declaration **fixed**, present and identical | Required | These are the identity keys plus whatever selection the author already made. |
| **Fills** a key the declaration left `${VAR}` | Allowed | That is what the template said it was for. It may also be left templated — a caller narrowing in two steps. |
| **Adds** a key the declaration never mentioned | Allowed | Adding always narrows: every reader of a `formal` either enforces a key or carries it, so a key the author did not write can only cost the caller peers, never gain it any. |
| **Drops** a fixed key | Refused | Broadening. `pow.block_id` removed turns "contains block B" into "any peer" — refused as firmly as an edit precisely because it is the one that *looks* harmless. |
| **Changes** a fixed key | Refused | A caller may complete what the author left open; it may not rewrite what the author decided. |
| Still carries an unfilled `${VAR}` | Refused | There is no peer holding a block named after a variable. The node deliberately does **not** fill one in from its own environment: which instance of the protocol is meant is the caller's decision, which is the entire reason the key was left open. |

The asymmetry is the whole rule: **templates and additions narrow; removals and edits
broaden; only narrowing is granted.**

A request is accepted if it fits **any one** of the caller's declared networks — a
service declares several and is asking about one. A rejection reports every
declaration it was measured against and why each refused it, for the same reason the
policy's rejection report names every declared network: a verdict on one of a set
says nothing useful alone.

#### When the caller cannot be identified

The caller is identified by its address against `local_instances.ip` — the same
identification `ModifyServiceSystemResources` already uses, reused so there is one
answer on this node to "which instance is this".

**"Cannot tell who is asking" is not "declared nothing".** A `ResolveNetwork` caller
is very often not a local instance at all: it is another celaut node asking as a
peer, which is the RPC's older use (`pow_networks._peer_suggested_endpoints` makes
exactly that call). Such a caller has no spec here to be measured against, the subset
check does not apply, and it is answered exactly as before. A caller that *is* a
local instance but whose spec cannot be read right now is treated the same way, and
logged — a deliberate difference from `filter_networks_with_ancestors`, which aborts
on that condition. It aborts because it decides what to **open** for a guest about to
run, where "cannot tell" must not read as "allowed". Here nothing is opened: the
answer is a list of addresses the caller verifies itself, so failing closed on a
transient memory-lock timeout would break resolution for an instance behaving
perfectly, and would buy nothing.

A caller that *is* identified and whose service declares **no network at all** is
refused outright: an empty declaration is "I asked for no domain", and resolving one
for it would make the check optional for anybody willing to declare nothing.

The operator's policy runs **first**, before the caller is identified, so a caller
learns "not from this node" before it learns anything about its own declaration.

* **Implementation:** `request_fits_declaration()`, `check_network_request()`,
  `declared_networks_of_caller()` in `src/manager/networks.py`; the handler in
  `src/gateway/gateway.py`
* **Tests:** `tests/test_resolve_network_subset.py`

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
