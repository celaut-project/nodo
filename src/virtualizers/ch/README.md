# Cloud Hypervisor as Virtualizer (Strong Isolation)

## Objective and Context

Integrate **Cloud Hypervisor (CH)** as a virtualization backend to execute services with **strong isolation** (microVMs over KVM), while maintaining the existing contract exposed by `src/virtualizers/interface.py`.

The primary motivation is to increase isolation compared to standard containers, reduce the blast radius of potential escapes, and harden boundaries between services without changing the API consumed by the rest of the system.

## Current State and Blockers

While a facade exists in `src/virtualizers/interface.py`, the system remains tightly coupled with Docker in several areas, preventing the selection of an alternative virtualizer without refactoring.

**Critical points to decouple:**
* `src/balancers/execution_balancer/execution_balancer.py` calls `src/virtualizers.docker.build` directly.
* `src/gateway/iterables/*` imports `src/virtualizers.docker.build` directly.
* `src/gateway/launcher/launch_service.py` imports `TransportProtocol` and `allow_connection` from the Docker module.
* `src/manager/manager.py` uses `TransportProtocol` from Docker.
* `src/virtualizers/interface.py` currently only delegates to Docker and only accepts `docker` in `_is_supported_virtualizer`.

**Consequence:** Even if a CH backend is implemented, it cannot be used until these points are moved to the common interface.



## Proposed Architecture (MVP)

Essential components to run a service in CH:
* **Base Guest by Architecture:** Kernel (`vmlinuz`) + minimal `initramfs`.
* **Per-Service Rootfs:** `rootfs.ext4` generated from `service.container.filesystem`.
* **`__config__` Injection:** Equivalent to `set_container_config.py` but for the rootfs (pre-boot or via virtio-fs).
* **Networking:** One TAP interface per VM + host bridge, following a "deny by default" policy.
* **State Persistence:** An index mapping `vmachine_id -> {pid, api_socket, tap, ip, rootfs}` for `kill/maintain/firewall` operations.

---

## Phased Plan: Milestones and Deliverables

1.  **Phase 0 – Decouple Docker & Interface Preparation:**
    * Route all calls through `src/virtualizers/interface.py`.
    * Move `TransportProtocol` to a neutral layer.
    * Register virtualizer type per instance in the DB.
    * Define CH configuration keys.
2.  **Phase 1 – CH Build:**
    * Pipeline to generate `rootfs.ext4` from `service.container.filesystem`.
    * Kernel/initramfs selection by architecture.
    * Artifact cache layout.
    * `is_built` logic based on the CH bundle.
3.  **Phase 2 – CH Execute:**
    * MicroVM creation via API socket.
    * Rootfs mounting and `__config__` injection.
    * Boot process and IP acquisition.
    * VM state persistence.
4.  **Phase 3 – Networking & Firewall:**
    * Rules per TAP interface or stable IP.
    * Allowlist for gateway and peers.
    * Compatibility with `allow_connection_*` from the common layer.
5.  **Phase 4 – Lifecycle & Hotplug:**
    * Mapping `mem_limit/cpu` to dedicated **cgroups v2** per VM.
    * Explicit "unsupported" semantics per field.
    * `kill/maintain/remove` implementation for VMs.
6.  **Phase 5 – Observability & Operations:**
    * Minimal per-VM logs.
    * Basic metrics (PID, uptime, mem).
    * Resource cleanup.
    * Compatibility with `commands instances` (including the `virtualizer` field).

---

## Technical and Operational Feasibility

Mapping of `src/virtualizers/interface.py` functions:

| Function | Feasibility | Implementation Detail |
| :--- | :--- | :--- |
| `is_built` | **High** | Check for CH bundle (rootfs + metadata + kernel). |
| `build` | **Medium/High** | Transform filesystem into bootable rootfs; map to base guest. |
| `execute` | **High** | Create microVM, config net, boot, and persist `pid/socket/tap/ip`. |
| `hotplug` | **Medium** | Via **cgroups v2** (`memory.max`, `cpu.max`); report status per field. |
| `kill` | **High** | Direct `SIGKILL` + cleanup of TAP, DNAT, and cgroups. |
| `maintain` | **High** | Validate process/socket health; penalize if missing. |
| `remove` | **High** | Dual mode: clean runtime if active; delete build bundle by `service_id`. |
| `remove_firewall_rule`| **High** | Operates on the TAP interface or the VM's persisted IP. |

**Host Operational Requirements:**
* KVM support (`/dev/kvm`) and access permissions.
* Networking modules (TAP/bridge) and compatible iptables/nftables policy.
* Capability to run `cloud-hypervisor` binaries and manage control sockets.

---

## Configuration & Interface Changes

* `_is_supported_virtualizer` must accept `cloud_hypervisor`.
* `TransportProtocol` must be moved to a neutral layer (not under `docker/*`).
* New configuration keys under `virtualizers.ch.*`:
    * Paths for `KERNEL`, `INITRAMFS`, and `BINARY`.
    * `NETWORK_MODE`, `BRIDGE_NAME`, `SUBNET`.
    * `SECURITY` policies (path confinement, device allowlists).

---

## Network Design (Final Decision)

### Model
We reject NAT (user-mode/slirp) in favor of **TAP interfaces attached to a host bridge (`br-ch`)**. This maintains the current network contract and allows firewall rules via the VM's IP.

### Deterministic IP Assignment
IPs and MACs are derived from the `vmachine_id` without DHCP to ensure stability.
1.  Hash the `vmachine_id`.
2.  Derive a local MAC with prefix `02:42:ac`.
3.  Calculate an offset based on the subnet size.
4.  Assign the IP: `Base IP + Offset`.



---

## Shared-Disk (virtiofs) Networks — Virtualizer Requirements

Some services need to **share a disk** between instances (e.g. one instance
writes data and another, with read-only access, analyzes it; or Hadoop/Spark
clusters). This is modeled as a **shared-disk network**: a service declares a
`Service.Network` whose `protocol_stack` advertises the `virtiofs` tag, and
whose content id — think `H(ABCD)` — is the sha256 of the fixed anchor blob
(`Service.Network.formal`) that each participating service writes to disk.

The **node-side** logic for this already exists (independent of the backend):

* Network identity, virtiofs detection and membership resolution:
  `src/utils/networks.py`.
* **Placement/co-location gating:** `execution_balancer` pins an instance that
  declares a virtiofs network to a host that can co-locate it with the other
  instances of that network (same node). See
  `filter_placements_for_colocation`.
* **Peer discovery:** the `GetNetworkInstances` Gateway rpc returns the
  co-located instances of a network to a caller, gated so the node only answers
  for networks the caller declares in its own spec.

The **backend-specific** mount is implemented in `src/virtualizers/ch/virtiofs.py`
and wired into the CH `execute`/`kill` lifecycle. Per virtiofs network an
instance declares:

1. **virtiofsd daemon per shared-disk network.** *(done)* The first instance of
   a network on this host starts a `virtiofsd` exporting the network's shared
   directory (under `${CACHE}/cloud_hypervisor/virtiofs/<network-id>/shared`),
   keyed by the network content id. Co-located siblings reuse the same daemon
   and socket (`ensure_network_backend` is idempotent).
2. **virtio-fs device per guest.** *(done)* `execute` splices a
   `--fs tag=<vfs-…>,socket=…` device per network into the cloud-hypervisor
   command and injects a guest mount plan (`/.__nodo_virtiofs`) listing
   `{tag, path, ro}` for each disk. **Guest-init contract:** the initramfs
   mounts each entry (`mount -t virtiofs <tag> <path>`, adding `-o ro` when
   `ro` is set) — the one remaining guest-side step.
3. **Anchor placement.** *(done)* The service's fixed `ABCD` anchor blob
   (`Service.Network.formal`) is written into the shared directory once, so
   instances holding the same data resolve to the same network id.
4. **Lifecycle & cleanup.** *(done)* `kill` reference-counts the network across
   the CH runtime states and stops the `virtiofsd` + removes its socket only
   when the last instance on this host goes away. The shared directory (the
   data) is preserved.
5. **Security.** *(done)* Each `virtiofsd` is confined to its own export dir via
   `--sandbox chroot` (configurable), directories are `0700`, deny-by-default —
   no cross-network access.

### Read-only shared disks
A service asks for a **read-only** mount by adding a read-only tag
(`readonly` / `ro`, see `networks.READONLY_PROTOCOL_TAGS`) to its own virtiofs
network declaration — no proto change, since network/protocol `tags` are
free-form (the same mechanism virtiofs itself rides on). Read-only is a property
of *this service's declaration*, not of the network identity: a read-write
writer and a read-only reader still resolve to the same `H(ABCD)` and share one
daemon + directory; the `ro` flag only changes that guest's mount options.

### Distributed seeding (opt-in)
`filter_placements_for_colocation` is now peer-aware. By default placement stays
*local-authoritative* (pin/seed locally — always safe). With
`virtualizers.ch.VIRTIOFS_DISTRIBUTED_SEEDING=true` the balancer probes peers
via `GetNetworkInstances`; if a peer already hosts *all* the declared virtiofs
networks the new instance is routed there to co-locate. Probes are best-effort —
a denied/unavailable peer is treated as "not hosting", so placement degrades to
the safe local-seed path and never breaks disk sharing.

> **Still open (intentionally unsolved):** *who* launches the very first
> instance of a network when **no** node hosts `H(ABCD)` yet (cross-node seed
> election). We simply seed locally, which is always safe; global seed election
> is left as future work.

---

## Success Criteria
* Services run with strong isolation without changes to the system API.
* `interface.py` supports both `docker` and `cloud_hypervisor` via configuration.
* Network and firewall maintain existing functional behavior.
* The system can operate entirely without Docker when CH is enabled.
