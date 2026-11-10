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

- `block_all` drops every NEW connection leaving the VM, of every protocol: TCP,
  UDP, ICMP and all other IP protocols. It is one rule with no protocol match.
- Each `allow` opens exactly one destination — the node gateway, DNS, an address
  resolved from a `Service.Network` tag, or one `uri` of a dependency the node
  launched for it. Allows are inserted *above* the blanket drop, so a later allow
  wins.
- A service whose network tag is `*` gets `allow_all_egress` instead: one accept
  for the whole VM, still above the drop.
- Return traffic is covered once, globally, by a `RELATED,ESTABLISHED` accept
  pinned to the head of the forward chain.
- A guest gets IPv4 only. All IPv6 that comes in on the guest bridge is dropped on
  the forward hook by one global rule (`policy.guest_ipv6_drop_rule`): a guest can
  set its own IPv6 address, and no per-VM rule matches it. With iptables, this rule
  is in the FORWARD chain of `ip6tables`.

The decision of *which* rules a policy consists of lives in
`src/utils/firewall/policy.py` as pure functions, separate from the code that
applies them.

## Where the rules live

| Table | Chain | Hook / priority | Contents |
|---|---|---|---|
| `inet nodo` | `input` | filter, **-5** | the gateway port accept |
| `inet nodo` | `forward` | filter, **-5** | guest egress policy, published-port pass-through, guest IPv6 drop |
| `ip nodo` | `prerouting` | nat, -100 | published-port DNAT |
| `ip nodo` | `postrouting` | nat, 100 | guest subnet masquerade |

NAT is a separate `ip` table because all of it is IPv4; filter is `inet`.

Every rule carries a comment that identifies what it is and who it belongs to:

```
nodo;vm=<vmachine_id>;block_all
nodo;vm=<vmachine_id>;allow;192.168.200.1:58614/tcp
nodo;vm=<vmachine_id>;dnat;tcp;57545
nodo;gateway;port=58614/tcp
nodo;masquerade;subnet=192.168.200.0/24
nodo;forward;related_established
nodo;forward;guest_ipv6_drop
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
| guest → anywhere off-node | routed `nodo-br-ch` → uplink | the forward chain, i.e. the policy above |
| guest → the node itself (`192.168.200.1`) | **local delivery**, input hook | the host's input rules, **not** `block_all` |
| guest → another guest | routed via the host (see below) | the forward chain |

The second row is why the node's own listening ports are exposed to guests as far
as `block_all` is concerned: it only ever writes to the forward chain. What a
guest can reach on the host is decided by the input hook — the node's gateway
port accept, plus whatever else the host's own firewall allows there. A host that
runs services on `0.0.0.0` should keep the guest bridge in a restrictive zone; see
below.

## Guest-to-guest

Two guests on `nodo-br-ch` share one L2 domain, so by default they would ARP each
other and their frames would be switched tap to tap, never reaching the forward
hook — the allow-list would be a no-op for the destinations that matter most.
Two settings, applied together, prevent that (`src/virtualizers/microvm/network.py`):

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
- The node **refuses to serve** on a port it has proven unreachable, and proves it
  before serving anything. See below.
- A refusal takes nodo's own accept rule back out. A port nothing will answer on
  does not stay open.
- `network.GATEWAY_PORT` never resolves to `auto`. No port is a hard error with
  operator instructions, never a plausible-looking default.

Reachability is proven, not assumed: the node builds a throwaway network
namespace with a veth enslaved to the guest bridge and connects from inside it
(`src/utils/firewall/reachability.py`). Checking from the host itself would prove
nothing — a connect to a local address goes over `lo`, which nearly every
firewall accepts unconditionally. The probe is tri-state: "could not run" (no
bridge yet, not root) is reported as unknown, never as closed.

### Who assigns it, and when

Assignment happens in exactly two places, both of which are asking for it:
`install.sh` (via `src/commands/assign_gateway_port.py`, once, as root, with the
operator watching) and the daemon's start path
(`ConfigManager.assign_gateway_port_if_unset`). It used to happen while the config
*loaded*, which meant any privileged `nodo <anything>` — down to the shell
completion helper the terminal runs on a Tab keypress — could pick a port, write a
rule into the host's firewall and edit `config.yaml`. Loading the config now only
reads it.

### Where the port is proven

Assignment (`assign_gateway_port`) opens the port in nodo's ruleset and stores it
in `config.yaml`. It does not decide whether the port is *reachable* — that is
answered once, in the daemon's start path, and a negative answer stops the node.

It was the other way round, and the reason it changed is worth keeping. Assignment
used to probe the port and refuse to store one it could not clear, which sounds
strictly safer and was not: nothing can be probed before the guest bridge exists,
and the bridge was created on the first instance launch. On a host with firewalld
and no bridge yet, every assignment was refused forever, and the only move left to
the operator was to pin a port by hand — which went through no check at all. The
guard did not prevent the bad state; it routed people into the one path with no
verification on it.

So the bridge is now brought up at daemon start (`ensure_guest_bridge`), before the
port is touched, and the decision is made where it can be acted on:

| Probe, at daemon start | Outcome |
|---|---|
| reachable | recorded as proven; the node serves |
| **not** reachable | **the node does not start**, nodo's accept rule is withdrawn, and the message names what rejects |
| inconclusive | warning, nothing recorded, node serves; the next start tries again |

The probe supplies its own throwaway listener where nothing is listening yet
(`provide_listener`), which is what `nodo doctor` uses to give a verdict on a
stopped node. In the start path the gateway is already listening, so it does not
need to.

A conclusive *yes* is cached in `<main.CACHE>/gateway_port_passed`, so a restart
does not rebuild a network namespace to re-answer the same question. The file names
the port and the boot it was proven in: a port edited in `config.yaml` invalidates
it (both the Python side and the TUI delete the file on any write to the key), and
so does a reboot — an operator who opened the port with `firewall-cmd --add-port`
and no `--permanent` loses the rule there, and a verdict that outlived it would
skip the one check that catches that.

Anything that leaves the operator with something to do is written to
`.gateway_notice` beside `config.yaml` and held back until the process exits, so
it is the last thing on the terminal rather than the middle. `install.sh` prints
that file as its final act for the same reason.

What the message says about *other* rulesets has three states, not two, because
"nothing else on this hook rejects" and "the ruleset could not be read" are
different findings (`RejectorScan`). `nft -j list ruleset` is the fragile part: a
host whose ruleset includes tables managed by iptables-nft — Docker's, typically —
can have the JSON listing fail while plain `nft list ruleset` works. Reporting that
as "nothing found" sent operators looking in the wrong place. `IptablesBackend`
answers *unreadable* by construction: `iptables -S` cannot see a native nft table,
so it has nothing to find and must not claim otherwise.

When a port is refused, the message ends with the one command that opens it —
provided nodo could work out which command that is. It still writes only nftables
or iptables rules and drives no front-end, but *reading* the host to see which
front-end is running costs nothing and turns "establish this property" into
something an operator can paste. Detection is deliberately narrow: the binary must
be present **and** report itself active (`firewall-cmd --state` says `running`,
`ufw status` says `active`), because an installed-but-stopped firewalld is not what
rejected the packet.

- **firewalld:** `sudo firewall-cmd --permanent --add-port=<port>/tcp && sudo firewall-cmd --reload`.
  When the guest bridge is in a zone of its own (below), the command also opens the
  port in that zone (`--zone=<zone>`), and the guest-only plaintext port's rule goes
  only there: the default zone never sees a packet from a bridge it does not handle.
- **ufw:** `sudo ufw allow <port>/tcp`

Where nothing was detected there is nothing to name — the ruleset may be a
hand-written `nft` file, a config-management template, or a container runtime's
doing — and nodo will not invent a command for a front-end that is not running.
It names the rejecting chains it actually read, and states the property instead:

> Inbound TCP *port* must be accepted on the netfilter input hook, with no other
> base chain on that hook rejecting or dropping it. Guests reach it from the guest
> subnet over the guest bridge.

Establishing that is the operator's, who knows what manages their firewall. The
worked examples for the two common owners are in the next section — including the
guest-bridge zone firewalld needs, which a bare `--add-port` does not cover.

None of this says anything about reachability from **outside this LAN**: no check
on the host can answer that (a connect from inside succeeds whether or not the
router forwards anything). That is `nodo nat-guide`.

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
iptables -I DOCKER-USER -o nodo-br-ch -j ACCEPT   # inbound to the guests
iptables -I DOCKER-USER -i nodo-br-ch -j ACCEPT   # replies and egress
```

Neither rule weakens guest confinement: the node's own drop runs at priority -5,
ahead of the `filter` table, and a drop is terminal.

**firewalld.** firewalld owns a native nftables table, and its
`inet firewalld filter_FORWARD` (priority filter + 10) ends in
`reject with icmpx admin-prohibited`. nodo's accepts for a guest (in `inet nodo`,
and in `ip filter` through the compatibility chain) end only their own chain, so
whatever firewalld's zones and policies do not allow is rejected afterwards. A guest
sees `EHOSTUNREACH` ("No route to host"), a DNS timeout, or a connect that hangs.
The compatibility chain cannot help: it lives in `ip filter`.

Three paths cross the forward hook, and firewalld treats each differently:

| Path | What firewalld needs | Bridge in no zone | Docs' old zone (`forward` only) |
|---|---|---|---|
| guest → guest (a parent and its child) | the bridge in a zone with `forward` (or a policy, below) | **rejected**: the default zone's forward rule names only its own interfaces | works |
| guest → Internet (network tag `*`, peers on other hosts) | a **policy** from the bridge's zone to the uplink's zone, target `ACCEPT` | works only while the uplink is listed in the default zone | **rejected** |
| outside → published port (DNAT) | nothing: `ct status dnat accept` | works | works |

(Measured on a node with firewalld 1.1.1 and real guests: a `*` guest, an untagged
guest, and a child of each. With `StrictForwardPorts=yes` in `firewalld.conf` the
DNAT row needs a forward-port of its own.) Guest → node (the gateway port) is the
input hook, covered by the zone's ports.

The setup that opens all of them — a zone of its own for the bridge, with the
gateway ports and `forward`, and a policy `nodo-egress` out of it:

```bash
firewall-cmd --permanent --new-zone=nodo
firewall-cmd --permanent --zone=nodo --change-interface=nodo-br-ch
firewall-cmd --permanent --zone=nodo --add-port=<GATEWAY_PORT>/tcp
firewall-cmd --permanent --zone=nodo --add-port=<PLAINTEXT_GATEWAY_PORT>/tcp
firewall-cmd --permanent --zone=nodo --add-forward
firewall-cmd --permanent --new-policy=nodo-egress
firewall-cmd --permanent --policy=nodo-egress --add-ingress-zone=nodo
firewall-cmd --permanent --policy=nodo-egress --add-egress-zone=ANY
firewall-cmd --permanent --policy=nodo-egress --set-target=ACCEPT
firewall-cmd --reload
```

`install.sh` does exactly this when firewalld is running (as root; without root the
commands join the list of steps that need it), and skips what is already there.
`NODO_SKIP_FIREWALLD=1` makes it leave firewalld alone. Policies need firewalld 0.9
or later, `--add-forward` 1.0 or later.

**The policy does not open the Internet to every guest.** It only stops firewalld
from rejecting what nodo already allowed. nodo's per-VM `block_all` drops a guest's
new connections in `inet nodo` at priority -5, and a drop ends the packet on the
whole hook — so a guest without `*` still reaches only the peers, children and
gateway it was granted. Tested: with `nodo-egress` in place an untagged guest still
times out on 1.1.1.1:443 and on DNS, while the `*` guest next to it gets through.

Why `ANY` and not the uplink's zone: a host whose uplink changes zone (Wi-Fi, a VPN,
a second NIC) keeps working, and on firewalld 1.1 a policy to `ANY` also covers
guest → guest. `--add-egress-zone=<uplink zone>` works too if you want it narrower;
then `forward` on the zone is what carries guest → guest.

Prefer this over `trusted`, which would expose every port the host listens on to
every guest.

**You are told.** When the daemon starts (and before each launch) it reads
firewalld's configuration for the bridge: its zone, that zone's `forward`, the
uplink's zone, and the policies. If either forwarded path would be rejected it
writes `.forward_notice` beside `config.yaml` (the commands above, ports filled in,
in `.forward_notice.cmd`). That raises the `guest_forwarding_firewalld` alert in
`nodo info`, `nodo status` and the TUI — readable without root — and `install.sh`
prints the notice last. A launch that grants `*` on such a host logs that firewalld
will reject the guest's egress, instead of only "allowed". It is a reading of the
configuration, not a probe: it says nothing when firewalld is stopped or cannot be
read, and it is cleared by the next start once both paths are allowed.
`sudo nodo doctor` is the authority: it sends a real guest → guest and guest →
Internet packet, names `inet firewalld filter_FORWARD` and what is missing when one
fails, and clears or rewrites the notice from what the packets did.

`nodo doctor` reports which foreign chains on the input hook can reject, and runs
the gateway probe — supplying a listener, so it gives a verdict with the node
stopped, which is when you are most likely to be running it.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `network.GATEWAY_PORT` | *(assigned on first root start)* | the port guests and peers reach the node at |
| `virtualizers.ch.NETWORK_BRIDGE_NAME` | `nodo-br-ch` | guest bridge |
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
- firewalld is the one foreign forward policy nodo reads and explains (zone,
  `forward`, policies). Any other owner of a native nftables table is found by the
  doctor's probes and its rejecting-chain scan, but not explained.
