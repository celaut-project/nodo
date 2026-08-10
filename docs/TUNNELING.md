# Service Tunneling

How a service running on a node is reached from outside **without publishing a
port for that service**. Only the node's gateway port has to be reachable.

---

## The two ways to reach a service

| Strategy              | Exposed ports               | NAT configuration               |
| --------------------- | --------------------------- | ------------------------------- |
| **Direct exposure**   | One per service + node port | A forwarding rule per service   |
| **Service tunneling** | Node port only              | One rule, for the node          |

Direct exposure is what the node does by default: each declared slot gets a host
port out of `network.FREE_PORTS_RANGE` and is published in the instance's
`uri_slot`. Tunneling does not replace that — it is an additional way in, and the
only one available when `network.DISABLE_EXPOSE_OUTSIDE` is set.

### Option 1: Direct exposure (NAT traversal)

1. Forward the relevant ports on your router to the machine running the node.
2. Declare the usable range in `config.yaml` — one or more `START`/`END` pairs:

   ```yaml
   network:
     FREE_PORTS_RANGE:
       - START: 10000
         END: 20000
   ```

3. Get a static IP or set up dynamic DNS so peers can find the node.

> **Drawback:** every service needs its own reachable port, which means more
> router configuration and a larger attack surface.

### Option 2: Service tunneling through the node

The node exposes `Gateway.ServiceTunnel`. A caller holding an instance's token
opens one stream per connection and gets a raw byte pipe to one of that
instance's declared slots. Nothing else needs to be reachable.

Tunneling needs **no configuration**: the RPC is part of the gateway, so it is
available wherever the gateway is.

---

## `ServiceTunnel` wire protocol

Transport is **beeRPC** (`bee_rpc`, protobuf buffers over gRPC/HTTP2) — the same
transport as every other gateway RPC. The stream is bidirectional and typed
`TokenMessage | bytes -> bytes`:

| Step | Direction       | Message                     | Meaning                                        |
| ---- | --------------- | --------------------------- | ---------------------------------------------- |
| 1    | caller → node   | `TokenMessage(token, slot)` | Handshake. Exactly one, first.                 |
| 2    | caller → node   | `bytes`                     | Payload, forwarded verbatim to the service.    |
| 3    | node → caller   | `bytes`                     | Whatever the service replies, verbatim.        |

* `token` — the instance token, i.e. the `ServiceInstance.token` that
  `StartService` returned (`TokenMessage.token` semantics, same as `GetMetrics`).
* `slot` — the target port as a **decimal string**, and it must be a slot the
  service declares (see *Slot validation*).

beeRPC delivers each message whole — one message in is one message out, never
merged or split. That property is what lets datagram slots work (below); on a TCP
slot the boundaries are meaningless anyway, because the service's own writes get
coalesced by its socket.

The tunnel closes when either side does. When the caller's payload stream ends,
the node half-closes its write side to the service, so a request/response
service sees EOF and can still answer before the socket goes away.

### Transports

The node-to-service leg follows the transport the slot declares in
`service.api.slot.transport`. The caller does not choose it.

**TCP** is a byte stream, as above.

**UDP** carries one datagram per beeRPC message in both directions, so datagram
framing survives the trip. Three differences are inherent to carrying datagrams
over a stream, and callers must expect them:

| | Bare UDP | Through the tunnel |
| --- | --- | --- |
| Loss / reordering | Possible | None on the beeRPC leg (it runs over TCP/HTTP2) |
| Latency | Direct | Head-of-line blocking can add some |
| End of exchange | No signal | Closes after `network.TUNNEL_UDP_IDLE_TIMEOUT_S` of silence once the caller stops sending |
| Unreachable service | ICMP | Not detectable at open time (no handshake) |
| Zero-length datagram | Legal | **Dropped** — beeRPC cannot represent an empty message; drops are counted and logged |

Code that depends on UDP being lossy will not see loss here.

### Failure reporting

A tunnel that cannot be established fails with gRPC `INVALID_ARGUMENT` and a
message saying why (unknown token, undeclared slot, no host-supported transport,
unreachable TCP service, not enough balance to open it). Establishment is *eager*:
the handshake, validation, charge and connect all happen before the first byte is
serialized, so a broken tunnel never looks like a service that simply had nothing
to say.

---

## Slot validation

The node connects to `<instance internal IP>:<slot>` only if `slot` is one of the
`internal_port` values published in the instance's `uri_slot` — which come from
the service's own `service.api.slot` declarations.

This is deliberately narrower than "any port of that microVM". Without the check,
a token would grant access to every port inside the VM, including internal ones
the service never meant to expose.

---

## Authorization

Possession of the instance token is the credential, the same rule `GetMetrics`
applies. Note what this does and does not mean:

* The token identifies **one instance**, and validation limits the reach to that
  instance's **declared slots**.
* The token is the instance id, which appears in logs, `nodo instances` and the
  TUI. Treat it as a bearer credential and don't hand it out.

---

## Metering

Relaying costs this node CPU, memory and bandwidth, so it is metered — the same
way `maintain` meters a running instance, and charged to the same payer: **the
tunnelled instance**. That is the right payer because holding its token is what
authorises the tunnel in the first place.

| Key | Default | Meaning |
| --- | --- | --- |
| `pricing.TUNNEL_OPEN_ERG` | `"0.00001"` | Charged once per tunnel. An instance that cannot pay it is refused up front with `INVALID_ARGUMENT`, before any socket is opened. |
| `pricing.NET_ERG_PER_GIB` | `"0.002"` | Charged per GiB relayed, counting **both** directions. |
| `costs.TUNNEL_CHARGE_INTERVAL_KB` | `1024` | How much traffic accumulates before it is billed. |

Set a price to `"0"` to stop charging for it. Prices are in ERG; see
[`PRICING.md`](PRICING.md).

Billing is incremental rather than at the end, since a tunnel has no fixed length
and a caller that never closes would otherwise relay for free. Running out of
funds closes the tunnel.

Two honest details:

* Traffic is accounted **after** it moves, never before, so data already written
  is never discarded for lack of funds. The cost is that an empty balance is
  noticed one block late — a tunnel can overrun by up to
  `costs.TUNNEL_CHARGE_INTERVAL_KB` before closing. Shrink the interval to tighten
  that bound.
* Whether an empty balance actually stops anything depends on `costs.ALLOW_DEBT`,
  which is `true` in the shipped config. With debt allowed, tunnels are metered
  and logged but not cut off.

### Peers can see these rates before using them

`tunnel_open_mu` and `net_mu_per_gib` are advertised to peers, together with the
per-resource rates and the scarcity ceiling, inside `mu_per_call` of the gateway
slot in this node's `Instance` — so a peer knows the prices before it negotiates
anything. They are in MU (1 MU = 1 nanoERG), which is what makes them comparable
between nodes. `nodo peers` shows them per peer under `[Rates]`.

They are **ceilings, not quotes**, and the price of a *specific service* still
comes from `GetServiceEstimatedCost`, which prices the actual resources requested.
There is deliberately no per-GiB or per-vCPU rate: the cost model weights resources
against current supply (`maintain_execution_cost`), so a fixed price per resource
does not exist to advertise. A rate of zero is omitted rather than published as
free.

The rates need no schema of their own: a receiving peer already stores that slot
verbatim in `peer.protocol_stack`, and `submit_to_ledger` rebuilds it for the
reputation JSON — so rates a peer advertised to us get republished on-chain by us.
The network therefore learns each node's rates *through its peers*, not from the
node itself.

---

## Using it

The `nodo tunnel` command turns a tunnel into a local port, so any ordinary
client can use it:

```bash
# Through the local node; prints the port it picked.
nodo tunnel my-instance 8080

# Fixed local port, then use it like any local service.
nodo tunnel my-instance 8080 --listen 9000
curl http://127.0.0.1:9000/

# A datagram slot.
nodo tunnel my-instance 5353 --udp --listen 5353

# Through a remote node. The token must be the one THAT node knows.
nodo tunnel abcdef1234567890 8080 --peer 192.168.1.10:4040
```

`--udp` selects the *local* socket type; the node picks the node-to-service
transport from what the slot declares, so the two must match to be useful.

With TCP each accepted connection gets its own stream, so concurrent clients
work. UDP has no connections, so traffic is keyed by source address: the first
datagram from an `ip:port` opens a stream, later ones reuse it, and the flow is
dropped after `--idle` seconds of silence. The listener binds to `127.0.0.1`
unless `--host` says otherwise.

---

## Tunnels the node opens for itself (delegated execution)

The case above is a person running a command. The node does the same thing on its
own behalf when it delegates a service to a peer.

When our client asks for a service and the balancer picks a peer, the peer answers
with an `Instance` whose `uri_slot` holds *its* addresses. Our client then connects
straight to them — which only works while those addresses are reachable from here.
Two ordinary configurations where they are not:

* the peer runs with `network.DISABLE_EXPOSE_OUTSIDE`, so it advertises the
  internal IP of its own bridge, meaningless outside its host;
* the peer is behind NAT and advertises a LAN address we do not share.

In those cases the node stands in for the service itself: one local listener per
declared slot, each tunnelling to the peer, and the `uri_slot` handed to our
client is rewritten to point at those listeners. The client keeps speaking its own
protocol to what looks like a local service.

**The proxy has to be on the caller's side.** The client speaks its service's
protocol, not beeRPC, so the peer cannot hand it anything tunnelled — only the
caller's node can offer a plain socket and do the encapsulating.

### Policy: `network.DELEGATION_TUNNEL_POLICY`

| Value | Behaviour |
| --- | --- |
| `auto` (default) | Tunnel only when the peer's advertised addresses do not answer from here. A client on the same network as the peer keeps talking to it directly, with no extra hop. |
| `always` | Tunnel every delegated instance. No reachability guessing, one extra hop always. |
| `never` | Always hand over the peer's own addresses. A client on another network will fail to connect. |

Two honest limits on `auto`: reachability is probed from the *node*, which only
approximates what the client can reach, and a UDP slot cannot be probed at all
(no handshake), so UDP slots are tunnelled rather than assumed fine.

### Lifetime

Listeners live as long as the delegated instance and are closed when it stops. The
instance stored in `delegated_instances.serialized_instance` is the **rewritten**
one — what the client was told is what gets persisted — so the same listeners can
be rebound on the same ports when the node restarts, and firewall cleanup targets
the address the client was actually given. A client holding an address cannot be
told about a new port, which is why the ports are pinned rather than reassigned.

---

## Data flow

Both users of the tunnel share the same shape — a local listener, a beeRPC stream,
a socket to the service. They differ only in who runs the listener:

```text
        ┌─ nodo tunnel (a person wants a local port)
        │  or the node itself (a delegated instance our client cannot reach)
        │
┌───────▼──────┐  beeRPC  ┌───────────────┐  TCP/UDP  ┌───────────────┐
│   listener   │ ───────▶ │  Node running │ ────────▶ │    Service    │
│ TCP or UDP   │ ◀─────── │  the service  │ ◀──────── │ declared slot │
└──────▲───────┘          └───────────────┘           └───────────────┘
       │                   only this port              internal IP,
   the client              is exposed                  never exposed

1. The client connects (or sends a datagram) to the listener.
2. The listener opens a stream and sends TokenMessage(token, slot).
3. The node validates the slot and connects to the instance's internal address
   over the transport that slot declared.
4. Payload is relayed both ways until either side closes (or, on UDP, until the
   exchange goes idle).
```

---

## Comparison

| Feature                   | Direct exposure                 | Service tunneling                |
| ------------------------- | ------------------------------- | -------------------------------- |
| Exposed ports             | One per service + node port     | Node port only                   |
| Router configuration      | One rule per service            | One rule, for the node           |
| Attack surface            | Larger                          | Smaller                          |
| Reachable surface         | Whatever was forwarded          | Declared slots of one instance   |
| Latency                   | Direct                          | One extra hop + encapsulation    |
| Transport                 | TCP and UDP                     | TCP and UDP (datagrams become reliable) |
| Scales with               | Free ports available            | Concurrent streams               |

---

## Security notes

* **In transit:** the tunnel inherits the gateway channel's transport security.
  The channel is plain HTTP/2 unless the gateway is deployed behind TLS — the
  tunnel adds no encryption of its own, so don't assume any.
* **Validated target:** the node checks the instance exists and the slot is
  declared before connecting anywhere.
* **No direct routing:** callers never address the instance's internal IP; they
  address the node, which does the connecting.

---

## Not implemented

Stated plainly, because earlier versions of this document described these as if
they existed:

* **QUIC.** There is no QUIC front end. It is planned as a separate,
  caller-to-node transport in its own server and would not change the
  node→service side documented here.
* **`network.service_tunneling`, `network.tunnel_protocol`, `network.ddns`.**
  These specific keys never existed. The tunnelling settings that do exist are
  `network.DELEGATION_TUNNEL_POLICY`, `network.TUNNEL_UDP_IDLE_TIMEOUT_S`, and the
  prices under `pricing.TUNNEL_OPEN_ERG` / `pricing.NET_ERG_PER_GIB` (see
  *Metering*, above). Dynamic DNS is real too,
  just under a plain top-level `ddns:` section rather than nested in `network.` —
  see [CONFIG.md](CONFIG.md).
* **A persistent tunnel registry.** An early design kept one long-lived tunnel
  per service in a `tunnels` table. That approach was dropped: a tunnel lives
  exactly as long as its stream, and the table is gone. Delegated endpoints do
  survive restarts, but their state rides along in `delegated_instances` rather
  than in a registry of their own.
* **A reachability check from outside.** DDNS publishing (`ddns.*`) and the router
  guide (`nodo nat-guide`) both exist, but nothing confirms from *outside* that the
  gateway port is really forwarded: a connection from inside the node's own network
  succeeds either way. `nodo info` and `sudo nodo doctor` report what resolves and
  whether the port is listening locally, which is as far as this host can get.
  Confirming reachability needs a peer to try connecting back.
* **IPv6** on the delegated-endpoint path.
* **Latency-aware routing** based on `service.container.resources`.
