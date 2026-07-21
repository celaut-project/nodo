# Service Tunneling

Connectivity documentation between nodes: strategies for exposing services to the Internet and enabling peer-to-peer communication.

---

## Connectivity Strategies

There are two ways for a node to expose its services externally:

| Strategy              | Description                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------- |
| **Direct Exposure**   | Each service exposes its own port; NAT traversal is required for every service.             |
| **Service Tunneling** | Only the node's port is exposed; all service traffic is encapsulated and routed through it. |

---

## Option 1: Direct Exposure (NAT Traversal)

To expose a node and its services directly to the Internet:

1. **Configure NAT traversal** on your router to forward traffic to the machine hosting the node.

2. **Open all ports** required by the services—not just the node's port.

3. **Define the available port range** in `config.yaml`:

   ```yaml
   network:
     FREE_PORTS_RANGE: "10000-20000"
   ```

4. **Obtain a static IP address** or configure **Dynamic DNS (DDNS)** so that other nodes can locate your node.

> ⚠️ **Drawback:** Every service requires its own exposed port, increasing network configuration complexity and expanding the attack surface.

---

## Option 2: Service Tunneling Through the Node (Recommended)

If you prefer to expose **only the node's port**, you can use the built-in service tunneling mechanism. All communication between services is encapsulated and transmitted through this single port.

### `ServiceTunnel` RPC Method

The node exposes the `ServiceTunnel` RPC method, which allows connections to any service hosted by a peer using the following parameters:

| Parameter  | Description                                    |
| ---------- | ---------------------------------------------- |
| `token_id` | Identifier of the destination service instance |
| `slot_id`  | Target service port or logical slot            |
| `payload`  | Raw data forwarded to the destination service  |

When a request is received, the node locates the destination service using its `token_id` and transparently forwards the `payload` to the corresponding `slot_id`.

### Supported Transport Protocols

| Protocol   | Based On                     | Best Suited For                                       |
| ---------- | ---------------------------- | ----------------------------------------------------- |
| **beeRPC** | gRPC over HTTP/2             | Structured communication, streaming, interoperability |
| **QUIC**   | UDP with built-in encryption | Low latency, real-time traffic, mobile connections    |

### Tunneling Requirements

* **Both nodes must support service tunneling.** The destination node must have tunneling enabled to accept incoming tunnels.
* Once established, **all service-to-service traffic** flows through these tunnels.

---

## Comparison

| Feature                   | Direct Exposure                 | Service Tunneling                   |
| ------------------------- | ------------------------------- | ----------------------------------- |
| Exposed ports             | One per service + node port     | Node port only                      |
| Network configuration     | Complex (NAT for every service) | Simple (single port)                |
| Attack surface            | Larger                          | Smaller                             |
| Requires Static IP / DDNS | Yes                             | Yes (node only)                     |
| Latency                   | Direct                          | Slight encapsulation overhead       |
| Scalability               | Limited by available ports      | Virtually unlimited (logical slots) |

---

## Configuration

### Discoverable Node (Accepts Incoming Connections)

```yaml
# config.yaml
network:
  node_port: 8443
  service_tunneling: true
  tunnel_protocol: "quic"  # or "beerpc"

  ddns:
    enabled: true
    domain: "my-node.example.com"
```

### Outbound-Only Node (Connects to Known Peers)

```yaml
network:
  node_port: 8443
  service_tunneling: true
  tunnel_protocol: "beerpc"
  # DDNS is not required if the node does not accept incoming connections
```

---

## Tunnel Data Flow

```text
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Service A  │────▶│   Node A    │────▶│   Node B    │────▶│  Service B  │
│   (source)  │     │ (tunneling) │     │ (tunneling) │     │ (destination)│
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘

Step 1: Service A sends data to Node A (local/loopback)
Step 2: Node A encapsulates: token_id + slot_id + payload
Step 3: Node A → Node B via beeRPC or QUIC
Step 4: Node B decapsulates and forwards to the corresponding slot
Step 5: Service B receives the data as if it were a direct connection
```

---

## Security

* **Encryption in transit:** beeRPC (TLS over HTTP/2) and QUIC (native encryption) protect all traffic exchanged between nodes.
* **Implicit authentication:** The `token_id` identifies the destination service instance, and the node verifies its existence before forwarding any data.
* **Network isolation:** Services never communicate directly with remote IP addresses; they interact only with their local node.

---

## Recommendation

> 💡 **Use service tunneling as the default communication mode.** It is simpler, more secure, and more scalable.
>
> Reserve direct exposure only for scenarios where the additional latency introduced by tunneling is unacceptable. *(Latency-aware routing based on `service.container.resources` is planned but not yet implemented.)*

If you'd like, I can also Rewrite this in a more native technical documentation style similar to Kubernetes, Docker, or gRPC documentation.
