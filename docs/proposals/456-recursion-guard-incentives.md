# Who carries the recursion guard, and why: a praxeological study

Working document for [#456](https://github.com/celaut-project/nodo/issues/456). The
issue asks for two things. One is to carry the recursion guard on every RPC that
re-delegates; that is done for StartService and described in
[`RECURSION_GUARD.md`](../RECURSION_GUARD.md). The two read-only queries ended up with a
cache instead (see the outcome below).
The other is this study: **does a node have a reason to forward the guard honestly, and
what happens to the network if it does not?**

Every claim about how nodo behaves cites the file and function it comes from, and was
checked against the code on `dev` at the time of writing. Every claim about why a node
would behave a certain way is an argument, and is labelled as one.

---

## Outcome: the read-only queries need no incentive

This study was written assuming `GetServiceEstimatedCost` and `GetResourceAvailability`
would carry the guard, and most of §3-§4 and option (b1)/(b2) is about getting a peer to
forward it on those two. The conclusion after review is that they should not carry it.

- **Their answer depends only on their content.** A quote does not depend on who asks,
  and a probe asks about resources. So a node does not need a token to know it met a
  question before: it hashes the content it received and keeps one of three states per
  hash (unknown, in progress, done). Same question while in progress: refused with
  "retry", which ends A → B → A after one lap. Same question done: served until the TTL.
  See [`RECURSION_GUARD.md`](../RECURSION_GUARD.md#the-query-cache-for-the-two-read-only-rpcs).
- **It is unilateral.** The cache is a node protecting itself with what it observes. It
  needs no forwarding peer to be honest, so the whole of §3 (why a node would carry,
  omit, reset or truncate a token) does not apply to queries. A peer that strips a token
  from a query changes nothing, because the token was never needed.
- **StartService keeps the token and the hop budget.** An execution is not an idempotent
  answer, two clients may start the same service at once, and each hop is prepaid. §3
  and (a) stand for it unchanged.
- **Today the in-progress refusal is latent.** Neither query asks peers while answering,
  so nothing can come back to the node asking. What the cache does now is deduplicate
  repeated quotes and probes, on both sides: a node answers a repeated question once, and
  `estimate_cost_on_peer` / `check_resource_availability_on_peer` do not ask a peer what
  they already hold.
- **Considered and not needed:** detecting a dishonest forwarder by asking through two
  clients and comparing. The cache gives the same protection without the extra calls.

Open items, none of which changes the decision:

1. **Canonicalization.** The key is built from the question as this node parses it, with
   unknown fields discarded and a deterministic serialization, and metadata hashes sorted
   (the parser collects them in a set). A new field this node learns later changes how it
   keys that question; that only costs a recompute.
2. **Freshness.** A remembered answer can be stale up to its TTL: 30 s for a quote, 5 s
   for availability. A quote is remembered after the network-policy check, so a policy
   change lags by one TTL. `activity_window` is applied outside the cache, so closed
   hours are never served from memory.
3. **A forwarder that changes the content forces a recompute.** That is correct for a
   different question, and the cache does not try to stop it. What bounds someone who
   varies queries on purpose is the same as in (b2): the price of each call. If a query
   ever relays, the entry bound (`network.QUERY_CACHE_MAX_ENTRIES`) and the per-client
   rate limit contain the memory it can fill.
4. **b1 and b2 only matter if a query starts asking peers.** The cache refuses a
   repeat, but a distinct question each time still fans out, so (b1)/(b2) below stay the
   answer to that. They are no longer needed for the queries as they behave today.

---

## 0. Method

The framing is the one the issue asks for: praxeology, the Austrian analysis of human
action. Applied to nodo, it rests on four premises.

1. **A node is the instrument of a purposeful actor: its operator.** The operator
   chooses which nodo build to run, how to configure it and whether to patch it. Nothing
   in the protocol can stop a patched node, so the useful question is never "what does
   the code do?" alone. It is also "what would an operator *choose* to make it do, given
   what that choice costs and earns?"
2. **Ends are subjective, but we can name the ones the system rewards.** We do not need
   to know what an operator ultimately wants. We only need the ends nodo pays out on and
   charges against: MU earned for work run (`spend_mu`, the deposit flows in
   `launch_service`), MU kept rather than spent, standing in other nodes' balancers
   (`docs/REPUTATION.md`), and keeping its own business facts private.
3. **Choices are made at the margin.** An operator does not ask whether the guard is
   good for the network. They ask whether *this* change, on *this* request, gains them
   more than it costs them. Costs that land on others count only as far as they come
   back to the operator.
4. **Knowledge is local and dispersed.** A node knows only what reaches it: its
   immediate caller, the bytes on the wire, its own books. A mechanism that needs a node
   to know something it cannot observe does not work, however good it looks on paper.

What follows sets out the actor's position (§1), the options it has (§2), the incentive
behind each option (§3), what happens to the system as a result (§4), what can be
detected today (§5) and the options ranked by cost and benefit (§6). §7 is the
recommendation.

---

## 1. The actor's position: what a node sees and controls

### 1.1 What it sees about a request

- **Its immediate caller, and nobody further back.** Every gated RPC identifies its
  caller by a `client_id` this node minted itself (`client_gate.require_caller`). A
  `client_id` carries no key (`docs/EXECUTION_RECEIPTS.md`, *Unilateral by decision*).
  It can be bound to the caller's node identity after the fact (`AssociateClient`,
  `associate_client_with_peer`), but that identifies the hop, not the originator. When a
  node delegates, the next node sees *it* as the client: `delegate_execution` sends
  `get_client_id_on_other_peer(peer)`, the delegating node's own account at that peer.
- **The guard, as plain bytes.** `RecursionGuard{token, remaining_hops}` is not signed
  by anyone (`protos/celaut.proto`). A receiver can check that it is well formed
  (`validate_token`) and that it is not already holding the same token
  (`Registry.add`). It cannot check where the token came from, or whether the hop
  count was honestly decremented.
- **Nothing about the node's version.** `Peer` announces addresses, reputation proofs
  and resources, but no protocol version (`celaut.proto`, `message Peer`). A request
  without `remaining_hops` could come from an old node or from a new one that dropped
  the field. The receiver cannot tell which, and #456 deliberately treats both the
  same, as a root at the receiver.

### 1.2 What it controls about a request it passes on

When a node passes a request on, `service_extended` builds its guard and the balancer
and `delegate_execution` send it (`src/utils/utils.py`,
`src/balancers/execution_balancer/execution_balancer.py`,
`src/gateway/launcher/delegate_execution/delegate_execution.py`). All of that is the
operator's own code, so an operator can make it send any guard, or none.

When a node *receives* a request, `RecursionGuard.__init__` decides whether to accept
it. That code belongs to the receiving operator, who can delete the check.

### 1.3 How money moves along a delegation chain

This is the part that sets the incentives.

- **Prepaid, hop by hop.** Before delegating, a node must hold enough balance on the
  peer, in its own account there (`delegate_execution`: `balance_on_other_peer(peer) <=
  cost` refuses). The originator pays only its first node. Each node after that pays the
  next one out of its own deposit.
- **The father is charged the selected candidate's price, not a markup.**
  `launch_service` charges `spend_mu(father_id, estimated_cost.cost)`. Here
  `estimated_cost` is the quote of whichever candidate the balancer chose, converted
  into local MU (`estimate_cost_on_peer` → `estimated_cost_for_local`). Ongoing usage is
  passed through the same way: `maintain_delegated_instances` re-reads the peer's meter
  and charges the father the difference. **Stock nodo takes no margin on delegated
  work.** The only spread is conversion rounding, and that is set to favour the
  delegating node (`modify_deposit`'s `round_up=amount_mu < 0`; `configuration_for_peer`).
- **A quote is the node's own price, not the price it would end up charging.**
  `GetServiceEstimatedCostIterable.generate` answers with `generate_estimated_cost` for
  this machine. If the node is then asked to run the work, its balancer may pick a peer
  and charge that peer's price instead. #456 is the groundwork for closing that gap: a
  quote that also compares the peers' quotes.
- **Selection is by score, not price alone.** `score(peer) = −ln(cost) + 2·r̂ + …` and
  `score(local) = −ln(cost) + LOCAL_BIAS + …` (`docs/REPUTATION.md`;
  `estimated_cost_sorter`). `r̂` is *this node's* own log of how each peer has treated
  it, so two nodes can rank the same pair of peers in opposite orders. This is what
  makes cycles possible even among honest nodes. A prefers B, B prefers A, and neither
  is wrong by its own lights.

### 1.4 What a node is punished for today

- **Its local reputation at each peer**, through `update_peer_reputation` with the
  reasons in `src/reputation_system/reasons.py`: payments acknowledged or not, refresh
  failures. No reason covers "sent me a cycle" or "reset a guard". The guard is
  invisible to reputation.
- **The per-client rate limit** (`client_gate._ClientCallWindow`): by default 120 calls
  per 60 s per `client_id`, then a 300 s quarantine. A node keeps one `client_id` per
  peer (`get_client_id_on_other_peer` caches it), so every call a node makes to a peer
  counts against that node's own budget there.
- **On-chain opinions** backed by burned ERG (`docs/REPUTATION.md`, `ô`). These are
  published by other operators, at their discretion, about whatever they observed.

---

## 2. What a node can do with the guard

Passing a request on, a node can:

| | Action | Wire effect |
|---|---|---|
| **F** | Forward faithfully | same token, `remaining_hops − 1` (what `recursion_guard_message` builds) |
| **O** | Omit it | no `RecursionGuard` at all; the next node mints a fresh root |
| **R** | Reset it | a new token, and/or full hops |
| **T** | Truncate it | same token, fewer hops than it was given (down to 1: "run it yourself, don't resell") |
| **I** | Inflate it | same token, more hops; the receiver clamps this to its own maximum (`RecursionGuard.__init__`), so it only matters to a receiver that removed the clamp |

Receiving a request, a node can:

| | Action |
|---|---|
| **H** | Honour it: refuse a held token, refuse zero hops (stock behaviour) |
| **D** | Disregard it: accept the work anyway |

O and R look the same on the wire, so they are analysed together below as **O/R**. A
fresh root is indistinguishable from an old node (§1.1).

---

## 3. Incentives, option by option

### 3.1 Forwarding faithfully (F): what it costs and what it earns

**Marginal cost to the forwarder: about zero.** Stock nodo already does it. The bytes
are negligible. Forwarding the token reveals nothing about the originator, because the
token is an opaque uuid.

The hop count does leak one fact. Roots start at the default 16, so a receiver given
`remaining_hops = 14` can infer that it sits two hops from a root. That means its
caller is reselling (more in §3.3).

**Marginal benefit to the forwarder: mostly protection from its own cycles.**
Suppose A forwards faithfully and the request comes back to A. A's own `Registry`
refuses it, A's caller (the node that looped it back) picks another candidate, and A
loses nothing. Without the guard, A would accept its own work back. That ends in one of
two ways:

- A runs it locally. A pays B (from A's deposit at B), B pays A (from B's deposit at A),
  and A runs the job it could have run in the first place. Two prepaid deposits are
  locked and two `START_SERVICE_ON_PEER_TIMEOUT` windows are spent (`delegate_execution`),
  for nothing.
- A delegates again. A's balancer still prefers B, B still prefers A, and the ping-pong
  continues until a deposit runs dry or a timeout fires at the top.

**The timeout is where the real loss is.** A caller waits at most
`START_SERVICE_ON_PEER_TIMEOUT` for a delegated StartService (`delegate_execution`). If
it gives up, it treats the delegation as failed, refunds its own father
(`delegate_execution` pops the `refund_container`) and moves on to the next
candidate. It never records the delegated instance,
which may still finish starting further down. That instance is then charged against
the giving-up node's deposit at the next node, and no one upstream will stop it or pay
for it. A
node inside a cycle therefore risks holding a funded child that nobody upstream will pay
for. A rational operator wants to avoid that. **This is the main reason a node keeps the
token: cycles cost the nodes inside them, and the token is the cheapest way to stay
out of one.**

The protection only works if the *other* nodes in the cycle forward the token too:
one of them has to recognise it. So the benefit to A depends on B's honesty, and B's
on A's. Holding the token is in each node's interest only if the others keep it too,
and nothing makes them.

### 3.2 Omit or reset (O/R): why a node might drop the guard

| Motive | Gain to the dropper | Does it pay? |
|---|---|---|
| **Win work a downstream guard would refuse.** A node that knows its preferred peer is already in the tree (it saw the token before) resets so the peer accepts. | The job goes to its preferred, cheaper peer instead of failing over to a worse one | Rarely. The node pays that peer and is reimbursed only the peer's own price (no margin, §1.3). It wins nothing except the chance to keep the job at all, and that chance is exactly what puts it in a cycle (§3.1) |
| **Hide the subcontracting chain.** A shared token lets two peers who compare notes, or one peer that sees it twice, link requests as one tree. The hop count shows a receiver that it is not the first node | Privacy of the operator's supply chain; no "your caller is a reseller" signal to downstream | Yes, a little. The gain is real if small. With stock pricing there is no margin to protect, so the downstream node gains little from knowing |
| **Extract margin** (patched node adds a markup) | The markup, on every request it resells | Only together with the next motive: a node that resells at a margin wants as many resellable requests as possible, and a budget limits how far a chain can reach |
| **Escape the budget**: a request at `remaining_hops = 1` may not be resold (`can_forward` is false; the balancer then quotes only `local`) | Can still resell a request it was told to run itself | Yes, for a margin-taking reseller with no local capacity. Without resale it has nothing to sell |
| **Look like an old node** | Plausible deniability | Free, because absence is excusable by construction (§1.1) |

The cost to the dropper is about zero, because no reason in `reasons.py` refers to the
guard and no RPC carries anything that would let a peer prove a reset. The cost to
everyone else is §4. **The table shows the gap: dropping the guard costs the dropper
nothing and occasionally gains it something, so honest forwarding rests on the
operator's goodwill and on the node's interest in avoiding cycles (§3.1), not on any
penalty.**

### 3.3 Truncate (T): a legitimate tool, not an attack

Lowering the hop count only shrinks the tree. A node that sends `remaining_hops = 1`
is telling its peer to run the job itself and not resell it, and there are honest
reasons to want that:

- Latency: each hop adds a `START_SERVICE_ON_PEER_TIMEOUT` window.
- Reliability: the node's `r̂` for the peer says nothing about whoever the peer would
  subcontract to.
- Accountability: the receipts design notes that a delegated execution can only be
  attested by the node that ran it (`docs/EXECUTION_RECEIPTS.md`, *Delegation*).

Truncation hurts nobody except the downstream reseller, which loses a resale. Nodo
should treat it as a feature (§6, option b3).

### 3.4 Disregard on receipt (D): the receiver's side

A receiver that accepts a looping token wins one more job. But by definition it is
already answering that tree. The job it is offered is, one or more hops later, the job
it already holds or paid someone else to do. Accepting it is a bet that the cycle pays
out before a timeout leaves it holding an unpaid child (§3.1). This is the same
calculation as §3.1 viewed from the other end, and it mostly favours honouring the
guard.

Disregarding *zero hops* is different. That receiver is not in a cycle, so for it the
request is plain new work. The guard costs it a sale, and **here the interests of the
individual node and the network diverge most clearly.** It is also the cheapest
deviation to commit, since it is a deleted `if`.

### 3.5 Summary of the private calculation

| Action | Private marginal cost | Private marginal benefit | Net, stock pricing | Net, margin-taking reseller |
|---|---|---|---|---|
| F | ≈ 0 | avoids own cycles (if others also forward) | **+** | small + |
| O/R | ≈ 0 (undetectable) | privacy; escapes the budget | ≈ 0 | **+** |
| T | ≈ 0 | latency, reliability, accountability | **+** where wanted | – (when it is the receiver) |
| D on loop | timeout / orphan risk | one more job | **–** | ≈ 0 |
| D on zero hops | ≈ 0 | one more job | small + | **+** |

The conclusion is plain. **With stock nodo there is no margin on delegation, so a node
gains almost nothing by breaking the guard and loses something by ending up in a cycle.
The guard is roughly self-enforcing.** Once operators take margins on resold work (a
patch away; nothing forbids it), dropping the guard and ignoring zero hops start to pay,
and nothing pushes back.

---

## 4. What happens to the system if the guard is not carried

| Failure | Mechanism in nodo | Who bears it | Bounded by |
|---|---|---|---|
| **Cycles** (A → B → A) | Each node prefers the next by its own score (§1.3); without the token, nothing breaks the ring | The nodes in the ring: locked deposits, timeouts, orphaned children (§3.1). The originator waits | Deposits: each hop must be prefunded (`delegate_execution`), so a ring stops when one runs dry. Timeouts at the top. Not by the protocol |
| **Unbounded subcontracting** (long acyclic chains) | Each node resells to a cheaper peer; no budget | The originator: latency grows with every hop, and accountability shrinks, because the node that runs the job is further away than any `r̂` the originator holds | The number of nodes, through the token (a token can be in each registry only once at a time). Without the token, deposits only |
| **Amplification** (fan-out) | Once quotes or availability probes ask peers (#456's motivation), each node asks every known peer, and each of those asks theirs: up to `N^H` calls for fan-out `N`, depth `H`. **The token does not bound this.** The balancer asks peers one after another, and a finished subtree releases its tokens, so later siblings can revisit the same nodes | Every node in the subtree pays CPU and rate budget. Quotes are free (no `spend_mu` on GetServiceEstimatedCost), so **the asker pays nothing for the subtree it triggers** | Only the hop budget, and the per-caller rate limit at each hop. The rate limit hits the node making the calls, not the one that reset the budget upstream |
| **Price stacking** | Each reseller adds a margin; the originator pays the sum | The originator, unknowingly: it sees one quote from its first node | Stock nodo has no margin (§1.3). A patched node is bounded only by competition: the originator's balancer still compares first-hop prices |
| **Capacity misreporting** | An availability probe that comes back to an ancestor is refused there, so its capacity looks like "unknown" | The admission decision (`evaluate_possible_environment_workloads`) | Avoided by design: the probe does not carry StartService's token (`RECURSION_GUARD.md`, *Why the workload-admission probe…*) |

Amplification is the important row. It has the classic shape of an externality:
whoever resets the budget pays one call per peer, and honest nodes pay for the subtree
below. Praxeologically, a cost the actor never bears does not enter the actor's
calculation, so goodwill cannot be relied on to prevent amplification, even though the
incentive against cycles is decent. **The default of 16 hops suits StartService chains,
which are prepaid at every hop, and is far too generous for free fan-out queries.**
§6 returns to this.

---

## 5. What is detectable or verifiable in nodo today

| Fact | Detectable? | How / why not |
|---|---|---|
| A request is a loop through *this* node | **Yes, if every node in the loop forwarded the token** | `Registry.add` → `RecursionLoop` |
| The guard was malformed | Yes | `validate_token` |
| A peer reset or omitted the token | **No** | A fresh token looks like a root; a missing field looks like an old node; there is no version field (§1.1) |
| A peer inflated `remaining_hops` | Partly | A value above the receiver's own maximum is clamped. Inflation within that maximum is undetectable |
| Who the immediate caller is | Yes | `client_id` (`require_caller`). Its node identity can be learned through `AssociateClient`; outbound channels verify the peer's certificate (`peer_channel` → `node_channel(expected_peer_id=…)`) |
| Who the originator is | **No** | Nothing on the wire names it. `client_id`s are per-node namespaces |
| That a node delegated at all | To its own father: yes, eventually (a delegated instance's endpoints are the peer's or a tunnel, `delegated_endpoints`). To others: no | – |
| How long a chain was | No | Timing is suggestive but noisy, and nodes control their own timestamps (`docs/EXECUTION_RECEIPTS.md`, *What receipts do not give*) |
| That someone paid for a hop | To the payee: yes (its own books, `Payable`). To anyone else: no | Deposits are off-chain balances on the payee's node, except for the on-chain `Payable` top-ups themselves |
| Signed statements by the executor | Not yet | Receipts are designed but not implemented (`docs/EXECUTION_RECEIPTS.md`, *Implementation path*) |

**In short, the only thing nodo can verify about the guard is a loop that every node in
it reported honestly.** Everything else is self-report. That rules out one class of
mechanism for now: anything that punishes a node for *resetting* the guard, because a
reset cannot be proven. What can be priced is what a node *does*: the calls it makes,
the work it accepts.

---

## 6. Options, ranked by cost and benefit

Each option is judged by what it costs to build and what it changes in the calculation
of §3.5.

### (a) Document and accept the risk: **this PR**

- **What:** the guard as shipped in #456 (token plus hop count, honoured by stock
  nodes), and this document.
- **Cost:** none beyond this PR.
- **Benefit:** with no margin on delegation, the private incentive already favours the
  guard (§3.5). Cycles are self-limiting through prepaid deposits and timeouts, and the
  people they hurt are the people who could prevent them.
- **Residual risk:** margin-taking patched nodes; future fan-out queries (§4,
  amplification); StopService/ModifyDeposit riding unguarded along recorded paths. A
  malicious executor can answer a StopService by calling StopService back. That is
  bounded only by the rate limit, but the same node could make those calls directly
  anyway, so it gains no leverage.

### (b) Cheap mechanisms

Ranked by cost/benefit, best first.

**b1. A smaller hop budget for free queries than for paid work.** *Cost: one config key
per RPC. Benefit: high.* Amplification (§4) is the one failure the incentives do not
fix, and it only affects free calls. A GetServiceEstimatedCost or GetResourceAvailability
that queries peers should start from a budget of 2–3, not 16: that is `N²`–`N³` calls,
instead of an exponent nobody would choose. StartService keeps 16, because each of its
hops is prefunded, which is a natural brake. Implement this as soon as either RPC
actually starts querying peers. The plumbing is already per-call: the root passes
`remaining_hops`.

**b2. Charge for quotes and probes that fan out.** *Cost: a small `spend_mu` on the
gated RPCs, using the client accounts that already exist. Benefit: high, and it targets
exactly the externality.* With a per-call price, a node that resets the budget pays for
every call it makes to its peers, and each honest node below pays for its own and passes
the cost up. This is option (b)'s "fee per hop" in its simplest form. It needs no
signatures, because it prices what a node *does*, not what it claims, and §5 says
claims cannot be verified. Price stacking then works *for* the guard: the further a
query travels, the more the originator pays, so a balancer keeps it short.

**b3. Make truncation available to operators.** *Cost: trivial. Benefit: medium.* A
`network.FORWARD_HOPS` (or per-request) cap lets a node say "whoever I delegate to runs
it themselves" (§3.3). It turns T, the one deviation that only ever *reduces* load, into
a supported option.

**b4. Count observed loops as a reputation event.** *Cost: a new
`Reason.RECURSION_LOOP` and one `update_peer_reputation` call where `RecursionLoop` is
raised. Benefit: low to medium.* It only fires when the guard *works*, and then the node
that looped it back was usually acting in good faith (§1.3: opposite `r̂`s). Use a small
negative weight, enough to break a persistent A ⇄ B preference. It cannot catch resets.

**b5. Require the guard for settlement.** *Cost: low. Benefit: low today.* A node could
refuse to delegate to, or refuse to accept, requests without a guard. But "no guard"
also means "old node", so this only becomes useful once old nodes have left the
network, and even then a reset passes. Defer.

### (c) Heavier mechanisms

**c1. An originator-signed guard.** The root signs `(token, max_hops, expiry)` with its
node identity, and each hop appends its own signature to a visited list: a signed path,
as in a path-vector protocol. *Cost: high.* It needs a new message, signature checks on
every hop, a way to verify a root that is a client rather than a node (clients have no
keys: `EXECUTION_RECEIPTS.md`, *Unilateral by decision*), and a policy for unsigned
legacy traffic. *Benefit: limited.* A reseller can still start a new signed root of its
own, which looks exactly like a legitimate new request. Signing proves the path a
request admits to, not that it has no other history. It becomes worthwhile only together
with c2.

**c2. Receipts that chain across delegation.** Once start receipts exist
(`EXECUTION_RECEIPTS.md`, *Implementation path*, phase 1), the executor's receipt can
include the guard it received. An originator holding a receipt chain A → B → C could
then prove that B reset the token, because C's receipt shows a different token than
A's. That turns §5's "No" into evidence a node can be denounced with on-chain. *Cost:
high, but receipts are already planned for their own reasons. Benefit: the only option
here that makes resetting provable.* Add the guard to the receipt payload when phase 1
is built.

**c3. Ledger-anchored hop accounting.** Each hop posts a commitment on Ergo. *Cost:
prohibitive* (a transaction per delegation hop). *Benefit:* nothing c2 does not already
give. Rejected.

---

## 7. Recommendation

1. **Accept the risk for paid delegation (a), as shipped.** Stock nodo takes no margin,
   a cycle hurts the nodes inside it, and hops are prepaid. Together these make
   forwarding the guard the rational default, and no current mechanism could catch a
   cheater anyway.
2. **Before any free RPC starts querying peers, do b1 and b2** (the queries themselves are
   covered by the cache, see the outcome above, but a distinct question each time still
   fans out): a small hop budget for quotes and probes, and a per-call price on them. Amplification is the only failure
   that the incentives in §3 do not discourage, because its cost falls on others. A
   price on calls makes each hop pay for what it triggers, and it rests on actions nodo
   can observe, not on claims it cannot verify.
3. **When execution receipts are built, add the received guard to the receipt (c2).**
   That is the point where resetting becomes provable and can be priced through on-chain
   reputation. Until then, a signed guard (c1) adds cost without adding proof.

Optionally add b3 (operator-set truncation) and b4 (a small reputation weight on
observed loops). Both are cheap and only ever reduce load.

---

## Appendix: the code paths this study rests on

| Claim | Where |
|---|---|
| Guard semantics (token, hops, clamp, refusals) | `src/utils/tools/recursion_guard.py` |
| Forwarded guard built in one place | `recursion_guard_message`; `src/utils/utils.py:service_extended` |
| No peer is selected for a delegated StartService once hops are spent | `execution_balancer` (`Registry().can_forward`) |
| Read-only queries are answered from a local cache by content hash | `src/utils/tools/query_cache.py`; `GetServiceEstimatedCostIterable`, `GetResourceAvailabilityIterable`; the asking side in `estimate_cost_on_peer`, `check_resource_availability_on_peer` |
| Caller identity is a per-node `client_id` | `src/gateway/client_gate.py:require_caller` |
| Delegating node is its peer's client | `delegate_execution` → `get_client_id_on_other_peer` |
| Hops are prepaid from the delegator's deposit | `delegate_execution` (`balance_on_other_peer` check) |
| Father charged the selected candidate's price | `launch_service` (`spend_mu(father_id, estimated_cost.cost)`) |
| Usage passed through at the peer's metered rate | `src/manager/maintain.py:maintain_delegated_instances` |
| Quote is the node's local price | `GetServiceEstimatedCostIterable.generate` → `generate_estimated_cost` |
| Selection score is per-observer | `estimated_cost_sorter`; `docs/REPUTATION.md` |
| Reputation reasons (no guard-related one) | `src/reputation_system/reasons.py` |
| Per-client rate limit | `client_gate._ClientCallWindow` |
| ResolveNetwork never relays | `src/manager/networks.py:resolve_network_for_peer` (`ask_peers=False`) |
| StopService / ModifyDeposit follow delegation records | `src/manager/manager.py:stop_instance`, `modify_deposit` |
| Receipts: design, delegation, implementation path | `docs/EXECUTION_RECEIPTS.md` |
