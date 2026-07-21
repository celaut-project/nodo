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

