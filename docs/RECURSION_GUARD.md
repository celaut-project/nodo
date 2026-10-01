# The recursion guard

Some Gateway RPCs can be answered by asking peers the same question: a node asked to
run a service may delegate it, and the peer it delegates to may delegate again. Without
a limit, that is how a request comes back to a node already working on it (A → B → A),
and how one call fans out into many. The **recursion guard** is the limit. It rides
along with every such request as a `RecursionGuard` message
([`protos/celaut.proto`](../protos/celaut.proto)) and is held by
`RecursionGuard` in [`src/utils/tools/recursion_guard.py`](../src/utils/tools/recursion_guard.py).

Issue [#456](https://github.com/celaut-project/nodo/issues/456) extended it from
StartService to every RPC that re-delegates, or will. Whether a peer has a reason to
carry it honestly is a separate question, covered in
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
| GetServiceEstimatedCost | the same envelope (`service_extended`) | index **2**, unchanged |
| GetResourceAvailability | `GetResourceAvailability_input_indices` | index **3**, new (1 = `ArchitectureResources`, 2 = `Client` as before) |

The hop count is a new field inside a message that already existed, so nodes that
predate it skip it as an unknown field. The new index on GetResourceAvailability is
different. bee-rpc refuses any index it was not told about, so an older node fails to
parse a request that carries one. Two rules handle that:

- `BeeClient.get_resource_availability` sends index 3 only when it has a token to
  forward. Requests with nothing to forward go out in the old shape.
- `workload_admission.check_resource_availability_on_peer` sees an older peer reject the
  index (`buffer head index is not correct`) and asks again without it. Nothing is
  lost: an older peer answers from its own machine and never passes the question on.

---

## Audit: which RPCs re-delegate (issue #456)

Every `Gateway` RPC in [`src/gateway/gateway.py`](../src/gateway/gateway.py), plus the
client-side code that calls peers on its behalf. "Re-delegates" means that while
answering, this node sends a request to another node: the same RPC or a different one.

| RPC | Re-delegates today? | To whom | Guard before #456 | Guard after #456 |
|---|---|---|---|---|
| **StartService** | **Yes**: `launch_service` → `execution_balancer` (a GetServiceEstimatedCost per peer), `delegate_execution` (StartService on the chosen peer), `_force_delegate`, and `evaluate_possible_environment_workloads` (GetResourceAvailability per peer) | any known peer outside the caller's network | Token accepted (index 2), registered by `RecursionGuard` while `launch_service` runs, forwarded to the balancer's quotes and to the delegated StartService. Loop refused. No depth limit. A launch requested by a local instance runs unguarded (`generate=False`) | Same token, same places, same loop refusal. Added: a hop budget (`remaining_hops`) that is accepted, validated, decremented and forwarded; a malformed token is refused; with no hops left the balancer asks no peers and only `local` is a candidate. The availability probe for descendant workloads deliberately does **not** forward the token (see below). Local-instance launches are unchanged |
| **GetServiceEstimatedCost** | Not today: it returns the local quote only (`GetServiceEstimatedCostIterable.generate`). A request for a hash it lacks does `add_wanted`, and the maintainer later fetches that hash with GetService. That fetch is asynchronous, one level deep and not repeated | (none synchronously) | Token accepted (index 2) and registered. A loop is refused, for example when the peer that asked you to run something then asks you for a quote under the same token. The token was **not** kept for any downstream call, and no hop count existed | Accepted, validated, registered with its hop count; loop, depth and malformed refusals. The effective token stays on `self.recursion_guard_token` while the quote is produced, so a future peer comparison is just `estimate_cost_on_peer(..., recursion_guard_token=self.recursion_guard_token)`. `service_extended` forwards it one hop less, and `Registry().can_forward` reports when no hops remain |
| **GetResourceAvailability** | Not today: it answers from `get_architecture_availability` alone | (none) | **None.** No index for it; the input was `Client & ArchitectureResources` (#459) | Accepted at index 3, validated, and registered with its hop count (loop, depth and malformed refusals). It is held in `self.recursion_guard_token` while answering. The client side is ready to forward it: `check_resource_availability_on_peer(peer, request, recursion_guard_token=…)` sends it one hop less, skips the peer when no hops remain, and falls back for older peers |
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

## Tests

[`tests/test_recursion_guard.py`](../tests/test_recursion_guard.py) covers:

- **The guard itself:** a missing or empty token is a root; a held token is a loop; no
  hops left is refused; malformed tokens are refused; hops are clamped; the forwarded
  token is unchanged and one hop less; the registry check-and-add is atomic.
- **Multi-node chains**, each node with its own registry: A → B → A and A → B → C → A
  are refused at A; the budget stops a chain; a node from before the field resets only
  the count, and the loop is still caught.
- **Each RPC that carries the guard.** For StartService through `launch_service`,
  `StartServiceIterable` and the balancer, the tests also check that existing behaviour
  is unchanged. GetServiceEstimatedCost is driven through its iterable.
  GetResourceAvailability goes over a real gRPC stream, including a server that
  predates #456.
