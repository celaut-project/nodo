# Onion routing for the gateway: a Tor backend

Working document. It has no issue number yet. The first part is an **audit** of the code
on `dev`. The second part is the **design** and the **implementation plan**. A visual
version of the plan is in [`onion-routing-tor-backend.html`](onion-routing-tor-backend.html).

## Goal

A node must be able to hide its IP address from the other peers.

- The network is small at the start. Anyone who learns the address of the first node
  learns its location. This stops a first group of operators from joining.
- Level 1 threat: the other peers. This plan covers it fully.
- Level 2 threat: a state adversary. This plan covers it in part. Tor does not protect
  against a global passive observer.
- Out of scope: the own outbound traffic of the node, and the economic layer. See §3.

Tor is the first backend. The design lets other backends (I2P, a mixnet) replace it
later without a change to the wire format.

## Why Tor and not an onion network built from nodo peers

The anonymity set of an onion network is the set of its relays. If the relays are nodo
peers, five nodes give almost no anonymity. This is the problem we want to solve.

Tor has about 7000 relays. Its anonymity set does not depend on the size of nodo. The
first node is hidden from day one.

An onion service also works behind NAT without an open port. A person with a laptop
behind a home router can offer resources.

Tor has central parts (the directory authorities). This is a risk of censorship and
availability. It is not a risk of de-anonymization. The backend interface (§2.1) keeps
nodo free to change.

---

## 1. Audit

All claims cite the code on `dev` when this document was written. Line numbers can
change.

### 1.1 What the node announces

| Step | Where |
|---|---|
| The announcement is built by `_unsigned_peer` | `src/gateway/utils.py:388-453` |
| The addresses come from `_uris_for_all_interfaces`: the public host first, then each non-virtual interface | `src/gateway/utils.py:154-216` |
| The public host is `network.PUBLIC_IP`, or the outbound IP from a UDP connect to `8.8.8.8` | `src/gateway/utils.py:98-118`, `src/utils/network.py:28-48, 113-124` |
| A value that is not an IP (a host name, so also `x.onion`) is returned unchanged | `src/utils/network.py:28-48` |
| The transport is the fixed tag `tcp` | `src/identity/transport_stack.py:106-118` |
| The stack is tls, http2, grpc, bee-rpc, celaut-gateway | `src/identity/transport_stack.py:499-506` |
| The signature covers `ip`, `port`, `expiry`, the transport and the stack, field by field | `src/identity/node_identity.py:523-541, 570-648` |
| A second announcement is stored on chain in Ergo register R9. It uses `PUBLIC_IP` | `src/reputation_system/contracts/ergo/transaction.py:197-247` |

The signature does not cover field names. It covers the text of each field. A new
transport tag needs no change to the digest.

### 1.2 What a node accepts

| Step | Where |
|---|---|
| `add_peer_instance` checks the signature and the anti-replay `ts` | `src/manager/manager.py:397-541` |
| `_store_peer_uris` filters each address twice | `src/manager/manager.py:318-384` |
| Filter 1: `resolve_slot_transport_protocols` reads the tags. Only `tcp` and `udp` are known | `src/virtualizers/firewall.py:15-82` |
| An address with only unknown tags is skipped | `src/manager/manager.py:351-356` |
| Filter 2: `speaks_our_transport_stack` compares the stack layer by layer | `src/identity/transport_stack.py:583-623` |
| The database keeps only `tcp` or `udp` in `uri.transport` | `src/database/sql_connection.py:1697-1733` |

An address with the tag `tor-onion-v3` alone is skipped by every node that does not
know it. This is the behavior we want. The node never resolves the `.onion` name with
the local DNS.

An address with the tags `tcp` and `tor-onion-v3` together would be read as `tcp`. An old
node would connect to the `.onion` name directly. The tags must never be mixed.

### 1.3 Where the node connects to a peer

All gRPC channels to a peer gateway are created in `src/identity/grpc_transport.py`.

- `_fetch_certificate` opens a socket for the TLS pre-check (line 90).
- `channel_and_peer_id` calls `grpc.secure_channel` (line 111).
- No other call to `grpc.secure_channel` or `grpc.insecure_channel` exists in `src/`.

The address list for a peer comes from `generate_uris_by_peer_id`
(`src/utils/utils.py:367-382`). It keeps only rows with an empty or `tcp` transport.
It also calls `is_open` (`src/utils/utils.py:393-449`), which opens a direct TCP socket.

`nodo connect` accepts a host name. `split_target`, `format_uri`, `uri_exists` and
`claim_uri` all parse a `.onion` name correctly. The calls that fail are the direct
network calls: `is_open`, `_fetch_certificate` and `grpc.secure_channel`.

### 1.4 Where the node listens

- The gateway TLS port is bound to `[::]` (`src/serve.py:379`).
- The plaintext gateway port is bound to the bridge address or loopback
  (`src/serve.py:402`). It is never announced.
- A scan of the Internet finds the TLS certificate of the node. The certificate is tied
  to the identity of the node.

### 1.5 How a caller is identified

`get_only_the_ip_from_context` (`src/utils/utils.py:167-177`) reads the IP of the caller.
`require_caller` (`src/gateway/client_gate.py:169-196`) and `ResolveNetwork`
(`src/gateway/gateway.py:150-153, 240`) use it to find a local instance. A connection
that arrives through a Tor onion service comes from `127.0.0.1`.

### 1.6 Service networks and egress

| Network | What happens today | Where |
|---|---|---|
| `"*"` | The VM gets a forward rule for all egress and the host masquerades it | `src/virtualizers/microvm/network.py:514-581`, `src/utils/firewall/policy.py:221-232` |
| `pow:<chain>` | The host checks each candidate with `requests`. The VM connects to the peer directly | `src/manager/pow_networks.py:621-641, 734-871` |
| DNS tag | The node resolves the name. The VM connects directly | `src/manager/networks.py:355-395` |
| Local members | Traffic stays on the bridge | `src/manager/networks.py:173-265` |
| Seeds | `service_networks.default_instances` | `src/manager/network_defaults.py` |

The policy checks run in this order: `filter_networks_with_ancestors`, then
`enforce_network_policy` (`src/virtualizers/microvm/rootfs.py:259-300`). They also run
before the balancer (`src/gateway/launcher/launch_service.py:225`) and in the cost
estimate (`src/gateway/iterables/estimated_cost_iterable.py:116`).
`enforce_network_policy` raises `NetworkPolicyRejection`. `ResolveNetwork` already turns
it into the message "This node does not reach that network".

Peers of a network are never reached through a tunnel today. Only delegated child
instances use `ServiceTunnel` (`src/gateway/launcher/delegate_execution/delegate_execution.py:26-55`,
`src/tunneling/delegated_endpoints.py`).

### 1.7 Instance addresses

`local_execution.py:367-415` chooses the address of an instance in this order:
`EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE`, the VM address, the node address on the
subnet of the father, then `network.PUBLIC_IP`. If the list is empty, the caller uses
`ServiceTunnel` (`protos/celaut.proto:466-469`).

`ServiceTunnel` carries UDP datagrams inside the gRPC stream
(`src/tunneling/tunnel_client.py`, `serve_udp`). UDP slots work over Tor through the
tunnel.

### 1.8 Configuration and tests

- `ConfigManager` reads `config.yaml` once (`src/utils/config.py:224`). A new section
  follows the idiom of `pow_networks` and `service_networks`. Load-time validation goes
  in `src/utils/config_validation.py`.
- Tests are `unittest` style files in `tests/`. Examples: `test_gateway_peer_uris.py`,
  `test_peer_identity_registration.py`, `test_network_policy_enforcement.py`,
  `test_peer_content_digest_covers_every_field.py`.
- The repository has no Tor, SOCKS or proxy code.

---

## 2. Design

### 2.1 The transport descriptor (option A)

The routing network is part of the transport of an address. It uses the same `tags`,
`prose` and `formal` as every other component.

```
Peer.Uri {
  ip:   "<56 characters>.onion"
  port: <virtual port>
  transport: {
    tags:   ["tor-onion-v3"]
    prose:  "TCP stream over a Tor v3 onion service."
    formal: "kind=stream\nport=per-address\nspec=rend-spec-v3"
  }
  protocol_stack: [tls, http2, grpc, bee-rpc, celaut-gateway]
}
```

Rules:

- The tag `tor-onion-v3` never appears with `tcp` or `udp` in the same transport.
- `formal` has `kind=stream` or `kind=datagram`. A reader uses it to match slots.
- The field `ip` holds a host name. Its name is now wrong. Rename it later. The
  field number does not change.
- The `.onion` name is not derived from the node key. Two protocols must not share one
  key. The signed `Peer` already links the `.onion` name to the identity.

A backend implements this interface:

| Operation | Tor |
|---|---|
| `transport_component()` | The descriptor above |
| `start()` and `stop()` | Run a `tor` process that the node manages |
| `open_socket(host, port, timeout)` | HTTP CONNECT to the local `HTTPTunnelPort` |
| `grpc_channel_options()` | `grpc.http_proxy` set to the `HTTPTunnelPort` |
| `listen(local_port)` | Create the onion service. Return the announceable address |
| `kinds` | `{"stream"}` |

### 2.2 Configuration

```yaml
routing:
  HIDDEN: false
  tor:
    ENABLED: auto
    BINARY: "tor"
    ANNOUNCE: false
    CLEARNET_VIA_EXIT: false
```

`validate_routing_config` rejects these cases:

- `HIDDEN: true` and no backend with `ANNOUNCE: true`.
- `HIDDEN: true` with `network.PUBLIC_IP` set.
- `HIDDEN: true` with `network.ANNOUNCE_PRIVATE_ADDRESSES` set.

### 2.3 Hidden mode

If `HIDDEN` is true, the node follows these rules.

1. The announcement has only the addresses of backends with `ANNOUNCE: true`. It has no
   clearnet address.
2. The register R9 on chain has the onion address. It never has `PUBLIC_IP`.
3. The gateway TLS port listens on `127.0.0.1` only.
4. Every call to a peer goes through a backend. If the backend is down, the call fails.
   There is no silent fallback to a direct connection.
5. The instances do not get a public address. The caller uses `ServiceTunnel`.
   `DELEGATION_TUNNEL_POLICY` is `always`.
6. The reachability probes are off.
7. `expiry_unix_timestamp` is 0 and the benchmark score is rounded.

An identity that was ever announced with an IP is burnt for hidden mode. The operator
must use a new identity.

### 2.4 Network policy in hidden mode

The user decision: if a network needs a connection to the outside, and that connection
needs a backend that the node does not have, the node rejects the network.

| Network | Hidden mode |
|---|---|
| `"*"` | Rejected |
| `pow:<chain>` | Rejected |
| DNS tag | Rejected |
| External seeds | Rejected |
| Local members | Allowed |
| Members on other nodes | Allowed in phase 4, through a tunnel |
| `ResolveNetwork` for `pow:` | Rejected |

The rejection uses `NetworkPolicyRejection`. The cost estimate answers "not supported".
The balancer then delegates to another node.

### 2.5 Threats that the backend does not remove

- **Traffic correlation.** An observer of the entry and the exit of a circuit can link
  them. Tor does not defend against a global passive observer.
- **Guard discovery.** The `tor` package must be 0.4.8 or later. It enables
  vanguards-lite for onion services.
- **Sybil relays.** A risk of the Tor network.
- **Leaks in the application.** Any nodo feature that sends a real address in the body of
  a message. Each new feature must be checked.
- **Trust by loopback.** See phase 2.

---

## 3. Out of scope

- The own outbound traffic of the node: Ergo explorer and node, Bitcoin, the energy
  meter, `internet_available`. These calls show the IP to the operator of the service,
  not to the other peers. Document this in the threat model.
- The economic layer. Payments and reputation proofs on chain are public. If the
  operator funds the identity from an account with KYC, the identity is linked to a
  person.

---

## 4. Implementation plan

### Phase 1: backends, and Tor for outgoing calls

The node can connect to `.onion` peers. The announcement does not change. This phase is
safe to release alone.

| Item | Where | Change |
|---|---|---|
| New package | `src/routing/{base,registry,clearnet,tor}.py` | `RoutingBackend` and a registry. The registry picks a backend from the transport tags or the host suffix |
| Tor process | `src/routing/tor.py` | Run `tor` with a data directory in `storage/tor/`. Open `SOCKSPort`, `HTTPTunnelPort` and `ControlPort` (cookie) on `127.0.0.1` |
| TLS pre-check | `grpc_transport.py:90` | Replace `socket.create_connection` with `backend.open_socket()` |
| gRPC channel | `grpc_transport.py:111` | Add the option `grpc.http_proxy` for a Tor address. The TLS pinning by peer id does not change |
| Probe | `utils.py:393` | `is_open` uses the backend. Use a longer timeout and cache for Tor |
| Peer intake | `manager.py:318` | Store `transport="tor-onion-v3"` if the registry knows it. Reject mixed tags |
| Address choice | `utils.py:367` | Select addresses with an active backend, in order of preference |
| Descriptor | `transport_stack.py:106` | Make `transport_component()` backend-aware. `nodo protocol` shows it |
| Install | `install.sh` | Install the `tor` package, version 0.4.8 or later |

Tests: registry, intake of a Tor address (with a simulated old node), CONNECT against a
fake proxy with a check that no DNS lookup occurs, the digest test.

### Phase 2: announce the onion address, and hidden mode

| Item | Where | Change |
|---|---|---|
| Onion service | `src/routing/tor.py` | Persistent `HiddenServiceDir` in `storage/tor/gateway/`. Back it up with the mnemonic |
| Announcement | `gateway/utils.py:154` | Add one address for each backend with `ANNOUNCE`. In hidden mode, add only those |
| On chain | `ergo/transaction.py:197` | Write the onion address in R9. Check how the reader of R9 parses it |
| Bind | `serve.py:379` | Bind the TLS port to `127.0.0.1` in hidden mode |
| Outgoing calls | `utils.py:367`, `grpc_transport.py` | Every call uses a backend. No fallback |
| Instance addresses | `local_execution.py:367-415` | Empty `uri`, use `ServiceTunnel` |
| Caller trust | `client_gate.py:169` | Use a separate listener for onion traffic. Tell the origin by port, not by IP |
| Probes and metadata | `reachability.py`, `gateway/utils.py:388` | Turn the probes off. Zero the expiry. Round the benchmark |

Tests: no IP in the announcement, bind to loopback only, R9 in hidden mode, empty `uri`,
and a test that a caller from `127.0.0.1` on the onion listener gets no instance identity.

### Phase 3: network policy in hidden mode

Apply the table in §2.4 in `enforce_network_policy` (`src/utils/network_policy.py`).
Reject `ResolveNetwork` for `pow:` networks in `resolve_network_for_peer`
(`networks.py:635`). Make the cost estimate answer "not supported".

Tests: a matrix of network type and `HIDDEN` in `test_network_policy_enforcement.py`.

### Phase 4: networks whose peers are instances on other nodes

This phase needs its own proposal.

1. A node learns the members of a network from its peers. A peer answers with an
   `Instance` that has an empty `uri`.
2. The node reuses `delegated_endpoints.publish`. It opens a local listener and forwards
   through `ServiceTunnel`. After phase 1, the tunnel goes through Tor.
3. The VM gets a firewall rule only to that local listener.

### Phase 5 (optional): a second backend

Add I2P (`i2p-stream`, `i2p-datagram`). This checks that the interface has no Tor
shape.

---

## 5. Open questions

1. **Issue number** for this work.
2. **A node-managed Tor or the Tor of the system?** Recommendation: managed. It has its
   own data directory, it does not touch `/etc/tor`, and it starts and stops with the
   node.
3. **Is calling `.onion` peers on by default?** Recommendation: yes (`ENABLED: auto`).
   If it is off, hidden nodes are reachable only for the few operators that turn Tor on.
4. **Is `CLEARNET_VIA_EXIT` off by default?** Recommendation: yes. Many exits block
   ports that are not standard, and availability then depends on the exit.
5. **Phase 4:** which credential opens a `ServiceTunnel` to an instance that the caller
   did not start? `ServiceTunnel` uses the token of the instance today.
6. **R9:** how does a reader of the on-chain announcement parse a non-IP address?
