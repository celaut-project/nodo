# Cloud Hypervisor as Virtualizer (Strong Isolation)

> **Historical design document (pre-migration).** This file describes the
> original plan to add Cloud Hypervisor *alongside* Docker and reflects a
> Docker-centric world that no longer exists. **Current reality:** Cloud
> Hypervisor (CH) is the only supported virtualizer; the Docker virtualizer
> has been removed (`src/virtualizers/interface.py` hard-returns `"ch"`).
> Treat references to Docker below as historical context, not current state.

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
We reject NAT (user-mode/slirp) in favor of **TAP interfaces attached to a host bridge (`nodo-br-ch`)**. This maintains the current network contract and allows firewall rules via the VM's IP.

### Deterministic IP Assignment
IPs and MACs are derived from the `vmachine_id` without DHCP to ensure stability.
1.  Hash the `vmachine_id`.
2.  Derive a local MAC with prefix `02:42:ac`.
3.  Calculate an offset based on the subnet size.
4.  Assign the IP: `Base IP + Offset`.



---

## Success Criteria
* Services run with strong isolation without changes to the system API.
* `interface.py` supports both `docker` and `cloud_hypervisor` via configuration.
* Network and firewall maintain existing functional behavior.
* The system can operate entirely without Docker when CH is enabled.

## Shared Filesystems (VirtioFS backend)

`virtiofs.py` materializes **parent → child shared filesystems** for CH microVMs.
This is purely a backend: the semantics (the `shared`/`guest`/`access` xattrs,
share identity, node co-location) live in `src/utils/shared_filesystems.py`, and
the service specification never mentions VirtioFS. See
[`docs/SHARED_FILESYSTEMS.md`](../../../docs/SHARED_FILESYSTEMS.md) for the full
model. Configure the daemon binary with `virtualizers.ch.VIRTIOFSD_BINARY`
(default `virtiofsd`).
