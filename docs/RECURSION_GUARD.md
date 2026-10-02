# The recursion guard

A node asked to run a service may delegate it, and the peer it delegates to may delegate
again. Without a limit, that is how a request comes back to a node already working on it
(A → B → A), and how one call fans out into many. The **recursion guard** is the limit
for StartService. It rides along with the request as a `RecursionGuard` message
([`protos/celaut.proto`](../protos/celaut.proto)) and is held by
`RecursionGuard` in [`src/utils/tools/recursion_guard.py`](../src/utils/tools/recursion_guard.py).

Issue [#456](https://github.com/celaut-project/nodo/issues/456) added the hop budget to it
and audited every RPC. The two read-only queries, GetServiceEstimatedCost and
GetResourceAvailability, do **not** carry the guard: a question's answer depends only on
its content, so a node recognises a question it has met by hashing it. That is a local
[query cache](#the-query-cache-for-the-two-read-only-rpcs) and needs no field on the wire.
Whether a peer has a reason to carry the guard honestly is a separate question, covered in
[`proposals/456-recursion-guard-incentives.md`](proposals/456-recursion-guard-incentives.md).

---

## What travels

```proto
message RecursionGuard {
    string token = 1;
    optional uint32 remaining_hops = 2;
}
```

- **`token`** names the whole request tree, and every node passes it on unchanged.
  A node registers the token for as long as it is answering, and refuses a second
  request with a token it already holds. That is the cycle check: A → B → A comes back
  to A with A's own token. A request **without** a token is a new root, and gets a
  fresh uuid4 hex.
- **`remaining_hops`** is how many nodes the tree may still enter, counting the node
  that receives it. A receiver given `h` registers `h`. It passes on `h − 1`, and stops
  passing it on once that reaches 0: it may still run the work itself. A receiver given
  `0` refuses. When the field is **absent**, the receiver uses its own
  [`network.RECURSION_MAX_HOPS`](CONFIG.md#network) (default 16). That covers a root
  request and anything relayed by a node older than the field. A value higher than that
  maximum is clamped down to it.

### Refusals

All three are subclasses of `RecursionRefused`. They are raised the way StartService
has always refused a loop, as an exception that reaches the caller as the RPC's error
status. Their messages:

| Exception | Message starts with | When |
|---|---|---|
| `RecursionLoop` | `Block recursion loop, recursion token:` | the token is already being answered here (unchanged from before #456) |
| `RecursionDepthExhausted` | `Recursion depth exhausted, recursion token:` | `remaining_hops` is 0 |
| `MalformedRecursionToken` | `Malformed recursion token` | the token is longer than 128 characters or uses anything outside `[A-Za-z0-9._:-]` |

The token alphabet covers every token this codebase mints (uuid4 hex) or is handed
(`nodo force_execution`, the benchmark core service). The length cap exists because a
held token sits in memory until the request ends, and the sender picks it.

### Where it sits on the wire

| RPC | Envelope | Guard at |
|---|---|---|
| StartService | `StartService_input_indices` | index **2**, unchanged |

The hop count is a new field inside a message that already existed, so nodes that
predate it skip it as an unknown field. No RPC got a new index, so no peer is affected
by the change.

(GetServiceEstimatedCost reads the same envelope and still parses an index-2 guard if an
older peer sends one, but ignores it: its answer comes from the cache.)

---

## Audit: which RPCs re-delegate (issue #456)

Every `Gateway` RPC in [`src/gateway/gateway.py`](../src/gateway/gateway.py), plus the
client-side code that calls peers on its behalf. "Re-delegates" means that while
answering, this node sends a request to another node: the same RPC or a different one.

| RPC | Re-delegates today? | To whom | Guard before #456 | Guard after #456 |
|---|---|---|---|---|
| **StartService** | **Yes**: `launch_service` → `execution_balancer` (a GetServiceEstimatedCost per peer), `delegate_execution` (StartService on the chosen peer), `_force_delegate`, and `evaluate_possible_environment_workloads` (GetResourceAvailability per peer) | any known peer outside the caller's network | Token accepted (index 2), registered by `RecursionGuard` while `launch_service` runs, forwarded to the balancer's quotes and to the delegated StartService. Loop refused. No depth limit. A launch requested by a local instance runs unguarded (`generate=False`) | Same token, same places, same loop refusal. Added: a hop budget (`remaining_hops`) that is accepted, validated, decremented and forwarded; a malformed token is refused; with no hops left the balancer asks no peers and only `local` is a candidate. The availability probe for descendant workloads deliberately does **not** forward the token (see below). Local-instance launches are unchanged |
| **GetServiceEstimatedCost** | Not today: it returns the local quote only (`GetServiceEstimatedCostIterable.generate`). A request for a hash it lacks does `add_wanted`, and the maintainer later fetches that hash with GetService. That fetch is asynchronous, one level deep and not repeated | (none synchronously) | Token accepted (index 2) and registered. A loop is refused. The token was **not** kept for any downstream call, and no hop count existed | **No guard. Query cache.** The answer is keyed by the hash of the service, configuration and metadata as received. Same question: priced once per `network.QUERY_CACHE_TTL_SECONDS`, with room for it checked fresh on every answer. Same question while it is being computed: waits for that answer (`network.QUERY_CACHE_WAIT_SECONDS`). Asking side: `estimate_cost_on_peer` keeps what a peer answered for `network.QUERY_CACHE_PEER_TTL_SECONDS` and does not ask it again. The token on the wire is accepted and ignored, and not sent |
| **GetResourceAvailability** | Not today: it answers from `get_architecture_availability` alone | (none) | **None.** No index for it; the input was `Client & ArchitectureResources` (#459) | **No guard. Query cache**, with a short TTL (`network.QUERY_CACHE_AVAILABILITY_TTL_SECONDS`, 5 s), forgotten whenever a local instance starts, stops or is resized. The wire is unchanged from before #456. `activity_window` is applied on top of the cached answer, so closed hours always win. Asking side: `check_resource_availability_on_peer` keeps what a peer answered |
| StopService | Yes, along a recorded path: `stop_instance` for a delegated instance calls StopService on the peer that runs it (`manager.py`, `BeeClient.stop_service`), and that peer may do the same | the one peer in the instance's `delegated_instances` row | none | **None, on purpose.** The next hop is a stored record, not a choice, and there is one per hop, so there is no fan-out. The chain is as long as the delegation chain StartService already built under the guard. The input is a lone `TokenMessage` that older nodes parse with a single index, so adding one would break every older peer on the most important cleanup call. Noted in the study as a residual risk |
| ModifyDeposit | Yes, along a recorded path: `modify_deposit` for a delegated instance calls ModifyDeposit on its peer | the one peer holding the instance | none | **None, on purpose.** Same reasoning as StopService |
| GetService | No. It serves from the registry. A miss is answered as a miss, and GetService never calls `add_wanted` | – | none (`service_extended(..., recursion_guard_token=None)` with a TODO) | Unchanged. If GetService ever fetches from peers on a miss, it has to take the guard first: its server already parses `StartService_input_indices`, so index 2 is available |
| ResolveNetwork | No: `resolve_network_for_peer` calls `resolve_network(..., ask_peers=False)`, so the question is never relayed | – | n/a (relaying is forbidden by design) | Unchanged |
| GetPeerInfo | No (answers from `generate_full_node_peer_info`) | – | n/a | Unchanged |
| IntroducePeer | No: `add_peer_instance` stores what it was given and calls nobody back. The reply reuses the `RecursionGuard` message as a carrier for the stored peer id, which is a TODO unrelated to recursion (the new optional field stays unset) | – | n/a | Unchanged |
| GenerateClient / AssociateClient / GenerateDepositToken / Payable | No (local bookkeeping and ledger checks) | – | n/a | Unchanged |
| ModifyServiceSystemResources | No: it hotplugs a **local** instance and is refused for any caller that is not one | – | n/a | Unchanged |
| GetMetrics | No: a delegated instance is answered from this node's own books (`get_delegated_balance`), not by asking the peer | – | n/a | Unchanged |
| ServiceTunnel | No: `_resolve_target` only reaches local instances. A delegated token has to be tunnelled through the node that runs it | – | n/a | Unchanged |
| Observe | No (local instance only) | – | n/a | Unchanged |
| Chat | No: `receive_chat_message` stores the message. Sending (`send_chat_message`) is operator-initiated and never triggered by a received message | – | n/a | Unchanged |

Background work that calls peers but is not an RPC answer: `maintain.py`'s wanted-service
fetch (GetService), peer refresh (GetPeerInfo) and delegated-instance billing.
`connect`'s IntroducePeer is also in this group. None of these re-enters the RPC that
triggered it, and none fans out recursively, so none carries a guard.

### Why the workload-admission probe does not forward StartService's token

`launch_service` asks peers whether they could host a service's declared *descendant*
workloads (`evaluate_possible_environment_workloads`). Those descendants are launched
later by the instance itself, and `launch_service` treats a launch from a local
instance as a new tree (`generate=False`). Forwarding StartService's token would give
the wrong answer. Every ancestor in the delegation chain is still answering that token,
so each would refuse the probe, and its capacity would be reported unavailable even
though it could host the descendants. The probe therefore goes out without a token, and
each peer it reaches starts its own root. If that probe ever recurses, the root's token
guards it from there.

---

## The query cache for the two read-only RPCs

[`src/utils/tools/query_cache.py`](../src/utils/tools/query_cache.py). A node keeps, per
question, one of three states:

| State | What the node does |
|---|---|
| unknown | computes the answer, holding the question *in progress* meanwhile |
| in progress | waits for that answer, up to `network.QUERY_CACHE_WAIT_SECONDS`, and serves it (single-flight). Past the wait, or when the thread computing it asks again itself, refuses with `QueryInProgress` (the caller sees the RPC error "retry") |
| done | answers from memory until the TTL runs out, or until `invalidate` drops it |

The key is the sha256 of the question's content as this node reads it: each message is
parsed back, stripped of fields this node does not know, and serialized deterministically,
so the order of fields (or a field the node would ignore anyway) cannot produce another key.
The set of metadata hashes is sorted first, as the gateway parser collects them in a set.
Who asked, and the guard, are not part of it.

What it gives:

- **The same question is answered once.** Repeated quotes and probes (`launch_service` prices
  the same service every time) stop costing a recomputation or a call to each peer.
- **A loop stops after one lap, if the question ever relays.** Neither query asks peers today,
  so this refusal is latent. If one later does, A → B → A arrives at A as a question A holds
  in progress: it waits at most `QUERY_CACHE_WAIT_SECONDS` and is refused, and the lap ends.
  Inside one process the same thread re-asking is refused at once. It needs nobody's
  cooperation, unlike a token.
- **Concurrent launches do not drop each other's candidates.** Two identical questions at once
  get the same answer; the second waits for the first instead of being told to retry.
- **A forwarder that changes the question gets it computed again.** That is the right answer
  to a different question. What bounds someone who varies it on purpose is the price of each
  call, not the cache.

Limits, on purpose:

- Entries are bounded (`network.QUERY_CACHE_MAX_ENTRIES`, least recently used first, never one
  in progress) and expire (`network.QUERY_CACHE_TTL_SECONDS`, `…_PEER_TTL_SECONDS`,
  `…_AVAILABILITY_TTL_SECONDS`).
  A TTL of 0 turns it off.
- A quote's price is remembered after the network-policy check, so a change of policy shows up
  up to one TTL late. Whether there is room for it is not remembered: `get_resource_availability`
  runs on every answer, so a full node never serves a remembered offer and a "no" is never
  remembered. `activity_window` is checked before the cache, so a closed node never serves one.
- Availability answers are dropped as soon as a local instance starts, stops or is resized
  (`SQLConnection.add_local_instance`, `purge_internal`, `update_sys_req`). One being computed
  at that moment is handed to its caller but not remembered.
- A peer's quote is reused for less time than this node's own (`…_PEER_TTL_SECONDS`, 10 s):
  the peer may already have served it from its cache, so the two stack.
- The asking side only recalls and remembers: it never refuses its own concurrent identical
  questions, which would drop a candidate peer from a second launch for no reason.
- StartService does not use it. Running a service is not an idempotent answer, two clients may
  start the same one at once, and every hop is prepaid, so it keeps the token and the budget.

---

## Tests

[`tests/test_recursion_guard.py`](../tests/test_recursion_guard.py) covers:

- **The guard itself:** a missing or empty token is a root; a held token is a loop; no
  hops left is refused; malformed tokens are refused; hops are clamped; the forwarded
  token is unchanged and one hop less; the registry check-and-add is atomic.
- **Multi-node chains**, each node with its own registry: A → B → A and A → B → C → A
  are refused at A; the budget stops a chain; a node from before the field resets only
  the count, and the loop is still caught.
- **StartService**, through `launch_service`, `StartServiceIterable` and the balancer,
  including that existing behaviour is unchanged and that a quote goes out without a token.

[`tests/test_query_cache.py`](../tests/test_query_cache.py) covers the cache: the three
states, errors not cached, TTL, the size bound, one computation under concurrent arrivals
(the others wait and get it), a waiter that computes when the first fails and refuses past
its wait, invalidation on local starts/stops, canonical keys, an A → B → A chain refused at
A, both RPCs through their iterables (including closed hours and a quote whose room went
away), and the asking side.
