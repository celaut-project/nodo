# Network members on other nodes, through tunnels the caller pays for

Proposal for phase 4 of [#539](https://github.com/celaut-project/nodo/issues/539)
([`onion-routing-tor-backend.md`](onion-routing-tor-backend.md), §4). It is also useful
outside hidden mode. It changes no code yet.

## The problem

A service declares a network (`Service.Network`). Today the node resolves it to two
kinds of peers:

1. **Endpoints**: `*`, `pow:<chain>`, host names and operator seeds. These are not
   celaut instances. They are chain nodes, APIs and the like, and the guest connects to
   them directly (or through Tor, for a hidden node).
2. **Local members**: instances that this node runs and that are members of the
   network (#387, `networks.local_network_instances`).

An instance on **another** node that is a member of the same network is not found.
If it were found, there would be no way to reach it:

- `Gateway.ServiceTunnel` opens a tunnel only with the **instance token**. Only the
  parent that started the instance has it, and the token must never leave the parent:
  it also stops the instance and spends its deposit.
- `TrafficMeter` charges the traffic to the **instance**, that is to the deposit of its
  parent. A stranger that could open the tunnel would spend the parent's money.
- The instance may have no address that the caller can reach (NAT,
  `DISABLE_EXPOSE_OUTSIDE`, a hidden node).

So, today, only the parent can tunnel to an instance, and only the parent pays.

## Rules for the design

1. **A service knows nothing about nodes.** The guest sees what it sees today: an
   `Instance` with `uri_slot` addresses, in `__config__.network_resolution` or in the
   answer to `ResolveNetwork`. No `peer_id`, no onion name, no handle. Where the member
   runs, and how the bytes get there, is the job of the node under the service.
2. **The caller pays for the traffic it causes.** Not the parent of the member.
3. **The member consents by its declaration.** An instance whose service declares the
   network and exposes a slot that speaks it (`networks._slot_exposes`, the rule for
   local members) accepts connections from other members of that network. Nothing else
   is exposed.
4. **The instance token never leaves the parent.**

## Design

### 1. A member handle, between nodes only

The node that runs a member (the **host node**) gives each member slot a **handle**: a
random 128-bit value, created when the instance becomes a member and deleted when the
instance stops. The host node keeps the map `handle -> (instance token, slot)`.

The handle only names the slot. It gives no right over the instance: it cannot stop
it, read its metrics or spend its deposit. Only a node sees it. A service never sees it
(rule 1).

### 2. Discovery: `ResolveNetwork` answers with remote members

When a peer (not a local guest) asks `ResolveNetwork` for a network, the host node also
answers with its local members. Each member comes as an `Instance` with:

- `api`: the slots that speak the network, as for a local member;
- `uri_slot[].uri`: empty;
- a new field, `Instance.Uri_Slot.handle` (string), with the handle of that slot.

`uri` stays empty on purpose: the asking node must not dial the member directly. A
node that does not know the new field sees an empty `uri` and does nothing with it.

The question is not relayed (`ask_peers=False`, as today). The asking node asks the
peers it chooses, and pays for that breadth itself.

### 3. `ServiceTunnel`: open by handle, charge the caller

The handshake gets a second form. The first message is one of:

| Form | Fields | Who may open | Who pays |
|---|---|---|---|
| Today | `TokenMessage(token, slot)` | Whoever has the instance token (the parent) | The instance |
| New | `MemberTunnel(handle, client_id)` | Any client of the host node | The `client_id`, from its deposit at the host node |

- `client_id` is the ordinary client of the caller node at the host node
  (`GenerateClient`, deposits). `TrafficMeter` takes a payer, an instance token or a
  `client_id`, and charges `TUNNEL_OPEN_MU` and `NET_MU_PER_GIB` to it.
- `slot` is not sent: the handle names it.
- An unknown or deleted handle is refused, and the reason is given.
- The host node can stop a tunnel when the member stops, when the client runs out of
  balance (`costs.ALLOW_DEBT`), or under `host_limits`.

The RPC set is declared in the `celaut-gateway` component of the transport stack
(`transport_stack.gateway_component`). A node that supports the new form says so there,
and a caller reads it before it uses the form.

### 4. On the caller node: a local listener, as for delegation

The caller node (the node of the guest that declared the network) does what
`delegated_endpoints` already does for a delegated instance:

1. It opens one listener per remote member slot, on its own bridge address.
2. Each connection to the listener becomes a `ServiceTunnel(MemberTunnel(handle,
   client_id))` to the host node, through the routing backend that reaches that node.
   A hidden node, or a host node that is only reachable by onion, uses Tor with no
   further change.
3. In `network_resolution`, the guest receives `ip:port` of the listener. To the guest
   it is an ordinary address (rule 1). The firewall gives the guest access to that
   listener (`allow_host_connection`).
4. The caller node pays the host node from its client deposit, and charges its own guest
   for the tunnel traffic, at its own rates. The guest pays the node it knows, and the
   node pays the next one.

### 5. Chains of tunnels

The member may itself be a delegated instance. Node B tunnels it from node C, and its
"address" at B is a `delegated_endpoints` listener. The host node B then maps the
handle to that listener, and the bytes go A -> B -> C. Each hop is a client of the next
hop and pays it, and charges the hop before it:

```
guest on A  --(listener on A)-->  A  --MemberTunnel(h_B, client A@B)-->  B
                                                    B  --tunnel (B's own)-->  C  --> member
```

No node needs to know more than the next hop, and no service knows that there is a
chain. Each hop adds latency and a margin to the price. A host node may announce a
handle only for members that it runs itself, to keep chains short. That is an open
question below.

## What this gives hidden mode

- A hidden node finds members of its networks on other nodes, and its guests reach
  them through Tor, with no address of the hidden node shown.
- A hidden node can be a host node: its members are reached through its onion address.
- Phase 3 can then allow "members on other nodes" in hidden mode (§2.4 of the plan).

## Changes, in order

1. Proto: `Instance.Uri_Slot.handle`, and `MemberTunnel { handle, client }` as the
   other form of the `ServiceTunnel` handshake. The gateway component formal changes.
2. Host node: the handle table (database, deleted with the instance), `ResolveNetwork`
   answers with remote members, `ServiceTunnel` accepts the new form, and `TrafficMeter`
   charges a payer that is not the instance.
3. Caller node: remote members in `resolve_network`, local listeners for them (reuse
   `delegated_endpoints.publish` with a handle in place of a token), and the charge to
   the guest.
4. Hidden mode: allow members on other nodes.

## Open questions

1. **Handle lifetime.** One handle for the life of the instance, or one per
   `ResolveNetwork` answer that expires? Per answer limits how long a handle seen by
   one peer stays useful, but it costs a table that grows with the questions.
2. **Who may ask.** Any peer with a `client_id`, or only a peer whose question shows it
   runs a member of the network? The second needs a proof the host node cannot check
   today.
3. **Chains.** Announce only members that the host node runs itself, or also delegated
   ones (§5)?
4. **Price of a member tunnel.** The same `TUNNEL_OPEN_MU` and `NET_MU_PER_GIB` as a
   parent's tunnel, or separate rates?
5. **Which peers to ask.** All known peers, the ones with the best score, or a limit
   per network? Each question costs the host node a lookup and, for the caller, a
   round trip (seconds through Tor).
6. **The tunnel of a parent.** Should the old form also charge the caller (the parent's
   `client_id`) and not the instance? The money comes from the same parent, but the
   accounting would then be the same for both forms.
