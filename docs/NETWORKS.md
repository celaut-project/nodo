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

The formal description (`formal`) encodes the consensus requirements used to select peers.

---

### 4. Generic Networks

> "Give me instances of the network with architecture X."

Applicable to any kind of network:

* **PoS:** Proof-of-Stake networks
* **P2P:** Arbitrary peer-to-peer networks

