# Firewall

How a node confines the services it runs, and what the host has to allow for a
node to work at all. Companion to [`TUNNELING.md`](TUNNELING.md) (reaching a
service without publishing a port) and [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

Every rule here is written by the node itself, as root, on each daemon start and
each instance launch. Netfilter rules do not survive a reboot; nothing in this
document is expected to be set up by hand except the host-owned parts in
[Sharing the host with another firewall](#sharing-the-host-with-another-firewall).

## The model

A guest starts with **no egress at all** and earns destinations one at a time:

- `block_all` drops every NEW connection leaving the VM, for TCP and UDP.
- Each `allow` opens exactly one destination — the node gateway, DNS, an address
  resolved from a `Service.Network` tag, or one `uri` of a dependency the node
  launched for it. Allows are inserted *above* the blanket drop, so a later allow
  wins.
- A service whose network tag is `*` gets `allow_all_egress` instead: one accept
  for the whole VM, still above the drop.
- Return traffic is covered once, globally, by a `RELATED,ESTABLISHED` accept
  pinned to the head of the forward chain.

The decision of *which* rules a policy consists of lives in
`src/utils/firewall/policy.py` as pure functions, separate from the code that
applies them.

## Where the rules live

| Table | Chain | Hook / priority | Contents |
|---|---|---|---|
| `inet nodo` | `input` | filter, **-5** | the gateway port accept |
| `inet nodo` | `forward` | filter, **-5** | guest egress policy, published-port pass-through |
| `ip nodo` | `prerouting` | nat, -100 | published-port DNAT |
| `ip nodo` | `postrouting` | nat, 100 | guest subnet masquerade |

NAT is a separate `ip` table because all of it is IPv4; filter is `inet`.

Every rule carries a comment that identifies what it is and who it belongs to:

```
nodo;vm=<vmachine_id>;block_all;tcp
nodo;vm=<vmachine_id>;allow;192.168.200.1:58614/tcp
nodo;vm=<vmachine_id>;dnat;tcp;57545
nodo;gateway;port=58614/tcp
nodo;masquerade;subnet=192.168.200.0/24
nodo;forward;related_established
```

So an instance's whole footprint is removed by prefix (`nodo;vm=<id>;`) on stop,
without replaying the arguments that created it. The masquerade is deliberately
global and never part of a teardown: removing it would cut every other running
instance.

On a host where nftables is the active backend, rules left in the `iptables`
compatibility tables by a pre-nftables node are swept once at start-up
(`src/utils/firewall/legacy.py`). `POSTROUTING` is excluded — a duplicate
masquerade is harmless, a missing one is not.

## The three paths out of a guest

Which hook a packet takes decides whether the policy above applies to it. This is
the part worth internalising:

| Traffic | Host path | Enforced by |
|---|---|---|
| guest → anywhere off-node | routed `br-ch` → uplink | the forward chain, i.e. the policy above |
| guest → the node itself (`192.168.200.1`) | **local delivery**, input hook | the host's input rules, **not** `block_all` |
| guest → another guest | routed via the host (see below) | the forward chain |

The second row is why the node's own listening ports are exposed to guests as far
as `block_all` is concerned: it only ever writes to the forward chain. What a
guest can reach on the host is decided by the input hook — the node's gateway
port accept, plus whatever else the host's own firewall allows there. A host that
runs services on `0.0.0.0` should keep the guest bridge in a restrictive zone; see
below.

## Guest-to-guest

Two guests on `br-ch` share one L2 domain, so by default they would ARP each
other and their frames would be switched tap to tap, never reaching the forward
hook — the allow-list would be a no-op for the destinations that matter most.
Two settings, applied together, prevent that (`src/virtualizers/ch/execute.py`):

- Each tap is enslaved as an **isolated** bridge port
  (`ip link set dev <tap> type bridge_slave isolated on`). An isolated port can
  exchange frames with the bridge itself, never with another isolated port.
- The bridge answers ARP on the neighbours' behalf: `proxy_arp=1`,
  `proxy_arp_pvlan=1` (the variant that replies on the interface the request
  arrived on) and `send_redirects=0` (otherwise the host tells the guest to
  shortcut directly to a neighbour, which isolation has just made impossible).

The guest still believes the whole subnet is on-link and needs no configuration
of its own. Every packet it sends to a neighbour goes to the host, is routed, and
is evaluated against the same rules as any other destination. Both settings are
required: isolation alone leaves a service unable to reach its own dependency.

Applied at tap creation and bridge preflight, so already-running instances keep
the settings they were launched with.

## Published ports

When an instance is exposed outside, the node allocates a host port and writes
three rules: the DNAT in `prerouting`, and a pair in `forward` for the translated
packet and its replies. `output` is deliberately not involved — it only sees
traffic the host itself originates, which is why a published port does **not**
answer from the node itself. Test it from another machine.

Publishing depends on the host being willing to *forward*. Nothing else does: a
node whose services are reached through [`ServiceTunnel`](TUNNELING.md) needs only
its gateway port.

## The gateway port

`network.GATEWAY_PORT` is the one port a node cannot work without: every
`node_controller` call a service makes goes through it. Its handling is
deliberately stricter than everything else (`src/utils/firewall/gateway.py`):

- The accept is re-applied on **every** daemon start, not once when the port is
  first assigned.
- A port is only persisted after it has been opened *and* proven reachable.
- `network.GATEWAY_PORT` never resolves to `auto`. No port is a hard error with
  operator instructions, never a plausible-looking default.

Reachability is proven, not assumed: the node builds a throwaway network
namespace with a veth enslaved to the guest bridge and connects from inside it
(`src/utils/firewall/reachability.py`). Checking from the host itself would prove
nothing — a connect to a local address goes over `lo`, which nearly every
firewall accepts unconditionally. The probe is tri-state: "could not run" (no
bridge yet, no listener, not root) is reported as unknown, never as closed.

## Sharing the host with another firewall

This is the part that needs the operator, and the reason is structural:

> In nftables, `accept` ends evaluation of **its own chain only**. The packet
> still traverses every other base chain on the same hook, and a `drop`, a
> `reject`, or a base chain's `policy drop` anywhere on that hook wins. No
> priority makes the node's accept authoritative.

So a rule the node writes is necessary but never sufficient. `drop` is the
opposite — terminal for the whole hook — which is why the guest isolation rules
at priority -5 are stronger than the host's own accepts, and why the node's
confinement of a guest does not depend on anyone else's cooperation.

Two common owners, and what each requires:

**Docker.** With iptables management enabled (the default), Docker sets the
`FORWARD` policy of the `filter` table to `DROP`. Its own rules accept traffic to
and from its bridges; a packet routed between any other pair of interfaces falls
to the end of the chain and is dropped. That covers every published port on this
node. The supported hook for an exception is `DOCKER-USER`:

```bash
iptables -I DOCKER-USER -o br-ch -j ACCEPT   # inbound to the guests
iptables -I DOCKER-USER -i br-ch -j ACCEPT   # replies and egress
```

Neither rule weakens guest confinement: the node's own drop runs at priority -5,
ahead of the `filter` table, and a drop is terminal.

**firewalld.** An interface bound to no zone is handled by the default zone,
which typically allows only ssh/cockpit/dhcpv6-client and ends in
`reject with icmpx admin-prohibited`. That reject applies to guest → node
traffic, including the gateway port, and a guest sees it as `EHOSTUNREACH`
("No route to host"). Give the bridge a zone of its own with just the gateway
port:

```bash
firewall-cmd --permanent --new-zone=nodo
firewall-cmd --permanent --zone=nodo --add-interface=br-ch
firewall-cmd --permanent --zone=nodo --add-port=<GATEWAY_PORT>/tcp
firewall-cmd --reload
```

Prefer this over `trusted`, which would expose every port the host listens on to
every guest.

Forwarded traffic that has been DNAT'd is accepted by firewalld regardless of
zone, unless `StrictForwardPorts=yes` is set in `firewalld.conf`.

`nodo doctor` reports the foreign chains on the input hook that can reject, and
runs the gateway probe.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `network.GATEWAY_PORT` | *(assigned on first root start)* | the port guests and peers reach the node at |
| `virtualizers.ch.NETWORK_BRIDGE_NAME` | `br-ch` | guest bridge |
| `virtualizers.ch.NETWORK_SUBNET` | `192.168.200.0/24` | guest subnet |
| `virtualizers.ch.NETWORK_GATEWAY_IP` | `192.168.200.1` | the bridge address, as guests see the node |
| `network.ISOLATE_INTERNAL_CHILDREN` | `true` | a child launched by a local instance is not exposed outside |

QEMU guests share all of it: the host side of the network is implemented once, in
the Cloud Hypervisor adapter, and reused.

## Known limits

- `block_all` covers the forward hook only, so it does not constrain what a guest
  can reach **on the node itself**. The per-VM allows for the gateway and DNS are
  written on the same hook and therefore have no effect on that path either; what
  the guest can actually reach on the host is whatever the input hook permits.
- A published port is never verified end to end. The node logs the DNAT it wrote;
  nothing connects from outside to confirm the host forwards it. The equivalent
  check exists only for the gateway port.
- Foreign chains are inspected on the input hook only, and only for reject
  *rules* — a base chain's `policy drop` on the forward hook is not reported.
