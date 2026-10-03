# Proposal: run nodo without `sudo`

Working document answering *"how can we avoid the sudo requirement?"*. It is a
**proposal**, not a change: nothing here ships with this PR.

It builds on [`ROOTLESS.md`](../ROOTLESS.md), the audit of 2026-08-02 against
`stable` @ `043d00a7`, and does three things that document does not:

1. **Re-audits against `dev` @ `c4e333ed`** (2026-10-03). Since August the QEMU
   backend, virtiofs shares, the nftables backend, the gateway port firewall,
   reachability probes, tunnels and `nodo config` have all landed, and several of
   them added privileged calls or `geteuid()` guards.
2. **Answers ROOTLESS.md's open questions by measurement.** Cloud Hypervisor v51.1
   *does* boot with zero capabilities, under one exact condition about the tap
   (§2.3). One claim in ROOTLESS.md turned out to be wrong (§2.1), and the probe
   found a latent bug that matters for any non-root plan (§2.2).
3. **Turns "Route A" into a phased plan** with a concrete systemd unit, an honest
   security assessment, and the operator experience at each step.

Every claim about the code carries a `file:line` against `c4e333ed`. Claims about
the kernel and the binaries are marked:

* **verified** — measured (see §2 for the setup);
* **docs** — taken from upstream documentation or kernel source, not executed here;
* **expected** — read from nodo's source, not executed.

---

## 1. TL;DR

* **Only one privilege is fundamental at runtime: `CAP_NET_ADMIN`** (taps, the
  bridge, nftables/iptables, `net.*` sysctls). Everything else on the runtime path
  is *ownership* or a *`geteuid()` guard*, not a kernel requirement:
  `/dev/kvm` is a group permission, cgroups are a delegation, the storage tree is
  file ownership, ports are already ≥ 50000.
* **Even `CAP_NET_ADMIN` can leave the hypervisor and the daemon.** Verified on
  aarch64 and on x86_64 (§2.5):
  `cloud-hypervisor` v51.1 run as an unprivileged user with **no capabilities at
  all** boots a nodo guest if the tap was created in advance, **owned by that
  user, and already UP**. So the privilege can be concentrated in a tiny helper
  that only creates/deletes taps and writes rules into nodo's own nft table.
* **Recommendation:** three phases. **(1)** stop needing `sudo` for everyday CLI
  commands — no change to the privilege model, just route them through the
  daemon. **(2)** run the daemon as a dedicated `nodo` system user with
  `AmbientCapabilities=CAP_NET_ADMIN` and `Delegate=yes`. **(3)** optionally move
  `CAP_NET_ADMIN` into a small `nodo-netd` helper so the daemon, Cloud Hypervisor
  and virtiofsd run with no capabilities. End state: **`sudo` once, at install
  time; never again for day-to-day operation.**
* **Honest caveat:** a process with `CAP_NET_ADMIN` can rewrite the host firewall,
  routes and `net.*` sysctls. Phase 2 is a big improvement over `User=root`, not
  "unprivileged". Phase 3 is what actually shrinks the blast radius.

---

## 2. What was verified, and how

**Setup.** The probes first ran in a throwaway **Ubuntu 24.04.4 LTS**
VM (kernel 6.8.0-117, systemd 255, cgroup v2, `aarch64`) on Apple Virtualization
with **nested virtualization**, i.e. a real `/dev/kvm` (`crw-rw---- root kvm`).
Binaries were the ones nodo installs: `cloud-hypervisor-static-aarch64` **v51.1**
and the pinned `guest-kernel` release (`vmlinuz-linux-arm64` `47ba56ef…`,
`initramfs-linux-arm64` `def84c27…`, both matching
`bash/guest-kernel/SHA256SUMS.pinned`). The unprivileged identity was `nobody`
plus the `kvm` group, with capabilities set by `setpriv`; the systemd test used a
transient `systemd-run` unit. Every link, table and cgroup the probe created was
removed and the VM deleted afterwards.

All of it was then **re-run on a real x86_64 + KVM node** (WSL2, see §2.5); every
result in §2.1–§2.4 held there. The only differences are WSL-specific and are
listed in §2.5.

### 2.1 Correction to ROOTLESS.md: `CAP_NET_ADMIN` *can* write `net.*` sysctls

ROOTLESS.md, Route A step 3, states that `sysctl -w net.ipv4.ip_forward=1` would
still fail under `CAP_NET_ADMIN` because it "does not bypass the DAC permissions
on `/proc/sys/...`". **Verified false for `net.*`:**

| As `nobody`, files `-rw-r--r-- root root` | no caps | `CAP_NET_ADMIN` only |
|---|---|---|
| `echo 1 > /proc/sys/net/ipv4/ip_forward` | Permission denied | **OK** |
| `echo 1 > /proc/sys/net/ipv4/conf/<bridge>/proxy_arp` | Permission denied | **OK** (value changed 0→1) |
| `kernel.printk_ratelimit` (non-`net`) | Permission denied | Permission denied |

This matches the kernel (docs): `net_ctl_permissions()` gives a holder of
`CAP_NET_ADMIN` in the network namespace's owning user namespace the *owner*
bits of a `net.*` sysctl. So Route A does **not** need a mandatory sysctl
workaround. Moving the settings into `/etc/sysctl.d/` is still worth doing (they
then survive reboots and are visible to the admin), but as hygiene.

### 2.2 Latent bug: `sysctl -w` exits 0 when the write is denied

**Verified:** procps-ng `sysctl -w` run without privileges prints
`sysctl: permission denied on key "net.ipv4.conf.<br>.proxy_arp", ignoring` and
**exits 0**; the value is unchanged.

nodo runs those writes through `run()` (`src/virtualizers/microvm/host.py:16`),
which trusts the exit code. Today the daemon is root, so it never bites. Under
*any* reduced-privilege model, `ensure_guest_l2_isolation()`
(`src/virtualizers/microvm/network.py:235-240`) — whose docstring says
"Failing here is deliberate. A half-applied setup is the worst outcome" — would
report success while proxy ARP stays off. Same for `ip_forward`
(`network.py:182`) and the bridge-netfilter keys in `_ensure_forward_compat`.

Fix (independent of everything else, ~10 lines): write the value to
`/proc/sys/...` directly, or read it back after `sysctl -w` and raise on
mismatch.

### 2.3 Cloud Hypervisor with no capabilities: what the tap must look like

ROOTLESS.md left open whether CH v51.1 starts with only ambient `CAP_NET_ADMIN`
and a pre-existing tap. **Verified** — and it needs less than that. CH run as
`nobody` + `kvm` group, **zero capabilities**, booting the pinned guest kernel and
initramfs with `--net tap=<name>,mac=…`:

| Tap prepared by root beforehand | Result |
|---|---|
| `ip tuntap add … user nobody` **and** `ip link set … up` | **boots** (`Run /init as init process`) |
| `… user nobody`, left DOWN | fails: `TapEnable(IoctlError(35092 …))` (35092 = `SIOCSIFFLAGS`: CH tries to bring it up) |
| no owner set, UP | **boots** — *any* local user could attach to this tap |
| `… user root`, UP | fails: `TapOpen(ConfigureTap(EPERM))` — owner mismatch |
| not pre-created (CH creates it), no caps | fails: `TapOpen(ConfigureTap(EPERM))` |
| not pre-created, ambient `CAP_NET_ADMIN` | boots |

So the contract for a capability-less CH is: **the privileged side creates the
tap with `user <nodo uid>`, enslaves it to the bridge, sets `isolated on`, and
brings it UP; CH only opens it.** Always set the owner: an ownerless persistent
tap is attachable by any user on the host (`/dev/net/tun` is `0666`).

The QEMU backend uses the same `network.create_tap()`
(`src/virtualizers/qemu/execute.py:531`) and opens it via `-netdev tap`
(`qemu/execute.py:314`); QEMU's `script=no,downscript=no` attach to an existing
tap has the same requirement (docs, not verified here).

### 2.4 Everything else that was measured

| Probe (as `nobody`) | no caps | `CAP_NET_ADMIN` | Notes |
|---|---|---|---|
| `ip tuntap add` | `TUNSETIFF: Operation not permitted` | OK | |
| `ip link add … type bridge`, `ip addr add`, `link set up` | `RTNETLINK: Operation not permitted` | OK | |
| `ip link set … master <br>`, `bridge_slave isolated on` | — | OK | what `create_tap()` does |
| `nft -f` a `nat` table with `masquerade` + `dnat` | `Operation not permitted (you must be root)` | OK | |
| `iptables -t nat -A POSTROUTING … MASQUERADE` | `Permission denied (you must be root)` | OK | |
| `ip netns add` / `unshare -n` | — | **fails** (`/run/netns` is root-owned; a new netns needs `CAP_SYS_ADMIN`) | affects the reachability probe, §3 row 8 |
| `open("/dev/kvm", "rb+")` | OK with `kvm` group | — | |
| `mkdir /sys/fs/cgroup/x` | Permission denied | — | root cgroup is root's |
| bind TCP `:52285` | OK | — | nodo's ports are 50000–60000 |
| `AF_PACKET` raw socket (`nodo observe` capture) | fails | fails | needs `CAP_NET_RAW` only — **verified OK** with just that cap |
| `unshare -Urn` | `write /proc/self/uid_map: Operation not permitted` | — | `kernel.apparmor_restrict_unprivileged_userns=1` |
| virtiofsd `--sandbox chroot` | `sandbox mode 'chroot' can only be used by root` | — | nodo's default, `virtiofs.py:146` |
| virtiofsd `--sandbox namespace` | OK once `uidmap` is installed and the user has a `/etc/subuid` range | — | same prerequisite the rootless packer already provisions |
| virtiofsd `--sandbox none` | OK | — | no confinement; not recommended |

**Transient unit = the Phase 2 unit, verified end to end.** A
`systemd-run -p User=nobody -p Delegate=yes -p AmbientCapabilities=CAP_NET_ADMIN
-p CapabilityBoundingSet=CAP_NET_ADMIN -p SupplementaryGroups=kvm` unit:

* got its own cgroup `/sys/fs/cgroup/system.slice/run-u22.service`, **owned by
  `nobody`**, with `cpuset cpu io memory pids` available;
* had to move itself into a leaf (`supervisor/`) first — cgroup v2's
  no-internal-process rule — and could then enable `+cpu +memory +pids` in its
  subtree;
* created `nodo-ch/vm-rl/`, moved a process in, and wrote `memory.max=268435456`
  and `cpu.max="50000 100000"` — exactly what `cgroups.py:103-128` does;
* created a tap; `CapEff = CapAmb = 0x1000` (only `CAP_NET_ADMIN`).

### 2.5 x86_64 (WSL2 on the Alienware) results

**Setup.** The live x86_64 nodo node: **Ubuntu 22.04.5 LTS** under **WSL2**, kernel
`6.6.87.2-microsoft-standard-WSL2`, systemd 249 as PID 1, cgroup v2 (root
`subtree_control`: `cpuset cpu io memory hugetlb pids rdma`), `/dev/kvm`
`crw-rw---- root:kvm`, `/dev/net/tun` `0666`. Binaries were the ones the running
node uses: `/nodo/bin/cloud-hypervisor` **v51.1** (static x86_64), guest kernel
`vmlinuz` `75f06d96…` (= `vmlinuz-linux-amd64` in `SHA256SUMS.pinned`, Linux
6.12.103), the node's installed `initramfs` (`ee89a955…`), virtiofsd 1.11.0,
nftables 1.0.2, iptables 1.8.7 (nf_tables), procps-ng 3.3.17. The unprivileged
identity was a throwaway user (no home, not in any group) plus the `kvm` group via
`setpriv`. Everything ran beside the live daemon without touching its bridge,
nft tables or cgroups; all probe links/tables/chains, the user and the temp dir
were removed afterwards.

| Claim (section) | aarch64 VM | x86_64 WSL2 |
|---|---|---|
| CH, zero caps, tap `user <uid>` + UP (+ enslaved) boots (§2.3) | boots | **confirmed** — `Run /init as init process` |
| same tap left DOWN | `TapEnable` `SIOCSIFFLAGS` EPERM | **confirmed** (`IoctlError(35092)`) |
| tap owned by root / by another user (`nobody`) | `TapOpen(ConfigureTap(EPERM))` | **confirmed** for both |
| ownerless tap, UP | anyone can attach | **confirmed** — boots |
| tap not pre-created: no caps / ambient `CAP_NET_ADMIN` | fails / boots | **confirmed** / **confirmed** |
| `CAP_NET_ADMIN` only: bridge, addr, up, tuntap, master, `isolated on` (§2.4) | OK | **confirmed** |
| `CAP_NET_ADMIN` only: own nft table with masquerade + dnat | OK | **confirmed** |
| `CAP_NET_ADMIN` only: iptables (own chain + rule) | OK | **confirmed** |
| `CAP_NET_ADMIN` writes `net.*` (`proxy_arp` on a probe bridge, file `0644 root`) (§2.1) | OK | **confirmed** (0→1→0, by `echo` and by `sysctl -w`) |
| `CAP_NET_ADMIN` writes `kernel.*` | Permission denied | **confirmed** denied |
| `ip netns add` / `unshare -n` with `CAP_NET_ADMIN` | fails | **confirmed** fails (`mount --make-shared /run/netns failed`) |
| `AF_PACKET` with `CAP_NET_RAW` only | OK | **confirmed** |
| procps `sysctl -w` on EPERM, no caps (§2.2) | prints "permission denied … ignoring", **exit 0** | **confirmed** — exit 0, value unchanged |
| `systemd-run User= Delegate=yes AmbientCapabilities=CAP_NET_ADMIN` (§2.4) | own cgroup owned by the user; leaf move; `memory.max`/`cpu.max` on `nodo-ch/vm-rl` | **confirmed** — `system.slice/run-u29.service` owned by the user, all controllers available, limits written, tap created, `CapEff = CapAmb = 0x1000` |
| virtiofsd `--sandbox chroot` as non-root | root only | **confirmed** root only |
| virtiofsd `--sandbox namespace` | needed `uidmap` + a subuid range | **differs:** listened even with no subuid range for the user (`newuidmap` already installed); also with one |
| `unshare -Urn` (unprivileged userns) | blocked by `apparmor_restrict_unprivileged_userns=1` | **differs:** allowed — the WSL kernel has no such sysctl |

**WSL-specific notes.** systemd delegation behaves exactly as on native Linux, so
the Phase 2 unit works on WSL as written; nft and iptables-nft are available and
coexist with nodo's own `inet nodo` / `ip nat` / `ip filter` tables. The two
differences above are permissive (an older procps/kernel/virtiofsd combination
and no AppArmor userns restriction), not new blockers. The guest initramfs on the
node does not match the pinned release hash because the node ships its own build;
the kernel does.

---

## 3. Inventory: every place nodo needs root today (`dev` @ `c4e333ed`)

"Kind" is the important column: **kernel** means the kernel enforces a capability
no ownership change can avoid; **ownership** means it is root only because root
owns the thing; **guard** means nodo itself refuses with a `geteuid()` check.

| # | Requirement | Where | Why | Kind | Least-privilege alternative | Effort |
|---|---|---|---|---|---|---|
| 1 | System packages (`apt-get`/`dnf`) | `bash/lib_pkg.sh:82,127,183`; `install.sh:111-116` | package DB | kernel/system | stays root, **once at install** | — |
| 2 | Install root `/nodo`, unit in `/etc/systemd/system`, wrapper in `/usr/local/bin` | `install.sh:16-17,356-361,368` | system paths | ownership | keep (system service is the right model); or `--target-dir` + user unit for single-user hosts | S |
| 3 | Daemon runs as root | `bash/nodo.service.template:7` (`User=root`) | everything below | ownership | `User=nodo`, caps + `Delegate=yes` (§5) | M |
| 4 | Guest bridge: create, address, up | `src/virtualizers/microvm/network.py:149-158` | netlink | **kernel: `CAP_NET_ADMIN`** | create once at install / by helper; or ambient cap | S |
| 5 | Per-VM tap: create, enslave, `isolated on`, up, delete | `network.py:262-280`; used by CH `ch/execute.py:470-471` and QEMU `qemu/execute.py:531` | `TUNSETIFF`, netlink | **kernel: `CAP_NET_ADMIN`** | ambient cap (Phase 2) or helper creating `user nodo` + UP taps (Phase 3, §2.3) | S / M |
| 6 | `net.ipv4.ip_forward`, bridge `proxy_arp`/`proxy_arp_pvlan`/`send_redirects`, forward-compat keys | `network.py:182,235-240`; `_ensure_forward_compat` `network.py:194` | `/proc/sys/net` | **kernel: `CAP_NET_ADMIN`** suffices (§2.1) | `sysctl.d` at install + runtime read-back (fixes §2.2) | S |
| 7 | nft/iptables: masquerade, DNAT, guest allow-lists, forward compat | `ensure_masquerade` `network.py:243`, `add_dnat_rule` `network.py:283`; `src/virtualizers/microvm/firewall.py`; `src/utils/firewall/backends.py` | nf_tables netlink | **kernel: `CAP_NET_ADMIN`** | ambient cap; or helper writing only into nodo's own table | S / M |
| 8 | Gateway port opened in `INPUT` | `src/utils/firewall/gateway.py:279` (guard `:304`); assignment guard `src/utils/config.py:620` | nf_tables | kernel cap, but **guard** is `geteuid()` | replace `geteuid()==0` with a capability check (`CapEff` has bit 12) | S |
| 9 | Reachability probes (`nodo doctor`, startup verify) | `src/utils/firewall/reachability.py:264,524` (`ip netns add`, `nsenter`) | new netns + `/run/netns` | **kernel: `CAP_SYS_ADMIN`** | keep root-only (it is a diagnostic), or do it in the helper; daemon reports "not proven" instead of failing | S |
| 10 | cgroups: `nodo-ch/<vm>` under `/sys/fs/cgroup`, enabling controllers at the root | `src/virtualizers/microvm/cgroups.py:9-10,87`; energy monitor `src/manager/energy/monitor.py:178` | root cgroup owned by root | ownership | `Delegate=yes`; base = own cgroup from `/proc/self/cgroup`, daemon moves itself to a `supervisor/` leaf (verified) | S |
| 11 | `/dev/kvm` | `src/virtualizers/ch/vgic.py:28`; doctor already checks a non-root service user `src/commands/doctor.py:113-131` | device node `0660 root:kvm` | ownership | `SupplementaryGroups=kvm` (verified) | S |
| 12 | virtiofsd `--sandbox chroot` | `src/virtualizers/microvm/virtiofs.py:146,297,374` | chroot needs root | **kernel** for that mode | `--sandbox namespace` (verified; needs `uidmap` + subuid, as the packer already does) | S |
| 13 | Control sockets in `/tmp/nodo-ch` | `src/virtualizers/microvm/paths.py:96` | shared `/tmp` | convention | `RuntimeDirectory=nodo` → `/run/nodo` (owned by `nodo`, mode 0750) | S |
| 14 | Storage tree (`/nodo/storage`, registry, cache, sqlite) written by root | `config.example.yaml:3-8`; `install.sh:468` | `install.sh` chowns `/nodo` to the *invoking* user, then the root daemon creates root-owned files inside it | ownership | `chown -R nodo:nodo` storage (`StateDirectory=` if relocated); group `nodo` read for operators | S |
| 15 | `config.yaml` made world-writable (`0666`) so both root and the user can write it | `install.sh:245`; `src/utils/config.py:906,919` | root/user split workaround | convention | `nodo:nodo 0660`, operators in group `nodo` — **should happen regardless of this proposal** (a root daemon should not read a world-writable config) | S |
| 16 | `nodo kill`, `burnall` | `src/commands/kill.py:27`; `src/commands/burnall.py:151` | `stop_instance()` runs **in the CLI process** (`kill.py:34` → `src/manager/manager.py:1172`) and touches taps/rules | guard | ask the daemon over gRPC, as `nodo execute` already does | M |
| 17 | `nodo remove`, `prune`, `prune_containers` | `src/commands/remove.py:48`; `src/commands/prune.py:119`; `nodo.py:911` | deletes root-owned files | guard + ownership | daemon RPC, or files group-writable by `nodo` | S |
| 18 | `nodo daemon start/stop/restart`; `nodo config set` restart | `src/commands/daemon.py:91,99-125`; `src/commands/config_edit.py:482` | `systemctl` on a system unit | ownership (polkit) | polkit rule: group `nodo` may `start/stop/restart` **only** `nodo.service` (§5.3); or a `Restart` RPC | S |
| 19 | `nodo doctor` | `src/commands/doctor.py:1180` | rewrites `/etc/systemd/system/nodo.service`, runs netns probes | genuinely root (repair tool) | split: read-only checks without root, `doctor --fix` with root | M |
| 20 | `nodo update` | `nodo.py:546` | re-runs `install.sh` | genuinely root | keep `sudo` | — |
| 21 | `nodo tunnels close` on another user's tunnel | `src/commands/tunnels.py:238` | signalling another uid | ownership | tunnels owned by the `nodo` user or managed by the daemon | S |
| 22 | `nodo observe` packet capture; conntrack | `src/commands/observe.py:20,748,1071,1233` | `AF_PACKET`; `/proc/net/nf_conntrack` | **kernel: `CAP_NET_RAW`** (verified, alone suffices) | daemon-side capture streamed over RPC; or keep "sudo for pcap" (metrics already work without) | M |
| 23 | Local packer (BuildKit) | `bash/start_buildkit_daemon.sh`, `bash/lib_rootless.sh` | — | **already rootless** (ROOTLESS.md) | `install_buildkit.sh` needs sudo once for `uidmap`/subuid | done |
| 24 | Firewall advice strings (`sudo ufw …`) | `src/utils/firewall/frontend.py:53-126` | host firewall | genuinely root, *operator* action | unchanged — that is the host admin's firewall, not nodo's | — |
| 25 | WSL distro logs in as root | `bash/setup_linux_x86.sh:477-478`, `bash/install.ps1:1178-1179` (`[user] default=root`) | convenience | convention | see §7 | — |
| 26 | Uninstall | `uninstall.sh:6`, umounts under `/nodo` `:78-84` | system paths | genuinely root | keep | — |

**Counted:** 39 `ip`, 2 `nft`, 1 `iptables`, 3 `sysctl`, 5 `systemctl` argv
call sites under `src/` (plus the firewall backend's generated argv), and 16
`geteuid()` checks. All of the `ip`/`nft`/`iptables`/`sysctl` sites reduce to
**`CAP_NET_ADMIN`**, except the netns probe (`CAP_SYS_ADMIN`).

**Not a requirement:** binding ports. The gateway and published service ports
come from `network.FREE_PORTS_RANGE` (50000–60000), so `CAP_NET_BIND_SERVICE` is
never needed. **Rootfs building** needs nothing (ROOTLESS.md: `mkfs.ext4 -d`,
`debugfs`, no `mount`/loop) — re-checked: no `mount`/`losetup` argv in `src/`.

---

## 4. The alternatives, weighed

| Option | Removes | Keeps | Tradeoff |
|---|---|---|---|
| **A. `nodo` user + `AmbientCapabilities=CAP_NET_ADMIN`** | root from the runtime; filesystem, process and module powers | `CAP_NET_ADMIN` in a Python process and every child it execs (CH, virtiofsd, `ip`, `nft`) | smallest code change; verified to cover taps, bridge, nft/iptables, `net.*` sysctls, cgroups with `Delegate=yes` |
| **B. `setcap cap_net_admin+ep` on binaries** | — | | rejected: putting it on `ip`/`nft` gives it to every local user; on the Python interpreter, to every script. A file cap is only reasonable on a *nodo-specific* helper binary (= C). |
| **C. `nodo-netd`: tiny privileged helper** (root or `CAP_NET_ADMIN`, socket-activated, `SO_PEERCRED`-checked) exposing only `create_tap(vm_id)`, `delete_tap`, `publish_port`, `allow`, `withdraw(vm_id)` | `CAP_NET_ADMIN` from the daemon, CH, virtiofsd | a small, auditable component | taps created `user nodo` + UP (exactly what §2.3 proved CH needs); rules only in nodo's own nft table, from the templates `src/utils/firewall/policy.py` already renders and validates |
| **D. Persistent taps pre-created at install** | the need for any runtime privilege for taps | | impractical as the *only* mechanism: the number of concurrent VMs is dynamic. Fine for the **bridge** (create once at install). |
| **E. User-mode networking (passt/pasta, slirp4netns)** | `CAP_NET_ADMIN` entirely | | CH has no built-in slirp/passt backend; passt's vhost-user mode or QEMU `-netdev stream` would be needed (docs, not verified). Throughput and latency cost; port publishing and the per-guest allow-list (`FIREWALL.md`) would be reimplemented in passt's forwarding instead of nftables. ROOTLESS.md "Route B". Long-term only. |
| **F. sudoers drop-in** (`%nodo ALL=(root) NOPASSWD: /usr/bin/systemctl restart nodo.service`) | the password prompt for restarts | `sudo` itself | acceptable fallback where polkit is absent; polkit is narrower and needs no `sudo` |
| **G. Docker group / rootful Docker for packing** | — | | moot: the packer is already rootless BuildKit. Do not reintroduce `docker` group (it is root-equivalent). |

---

## 5. Recommendation: phased plan

### Phase 0 — bugs and hygiene (independent, S, no behaviour change for root installs)

1. **Sysctl read-back** (§2.2): make `ip_forward`/`proxy_arp*`/`send_redirects`
   failures real failures.
2. **`config.yaml` not world-writable** (row 15): `0660`, owned by the service
   user, group `nodo`.
3. **Control sockets to `/run/nodo`** (row 13) instead of a fixed `/tmp/nodo-ch`.
4. **Replace `geteuid() == 0` with "has `CAP_NET_ADMIN`"** where the operation is
   a network one (rows 8, 9's caller), so Phase 2 does not trip nodo's own guards.

### Phase 1 — no `sudo` in everyday CLI use (M, privilege model unchanged)

* `kill`, `burnall`, `remove`, `prune`, `prune_containers` call the daemon over
  gRPC (or a local Unix socket, `/run/nodo/control.sock`, mode `0660`
  `nodo:nodo`, authorised by `SO_PEERCRED` group membership) instead of acting
  in-process. The daemon is already root; the CLI no longer needs to be.
* Install a **polkit rule** so group `nodo` may start/stop/restart
  `nodo.service` and nothing else:

  ```js
  // /etc/polkit-1/rules.d/50-nodo.rules
  polkit.addRule(function (action, subject) {
      if (action.id == "org.freedesktop.systemd1.manage-units" &&
          action.lookup("unit") == "nodo.service" &&
          ["start", "stop", "restart"].indexOf(action.lookup("verb")) >= 0 &&
          subject.isInGroup("nodo")) {
          return polkit.Result.YES;
      }
  });
  ```

  `nodo daemon restart` and the `nodo config set` restart then work as the
  operator. Where polkit is unavailable (minimal images, some WSL rootfs), a
  sudoers drop-in scoped to those three commands is the fallback (§4 F).
* `install.sh` creates group `nodo` and adds the invoking user (`$SUDO_USER`).

**Operator after Phase 1:** `sudo` for install, `update`, `uninstall`,
`doctor --fix`, and `observe` packet capture. Nothing else.

### Phase 2 — the daemon stops being root (M)

```ini
[Service]
User=nodo
Group=nodo
SupplementaryGroups=kvm
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
Delegate=cpu memory pids
RuntimeDirectory=nodo
# StateDirectory=nodo   -- only if storage moves to /var/lib/nodo (open question 2)
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
DeviceAllow=/dev/vhost-net rw
ProtectSystem=strict
ReadWritePaths=/nodo/storage /run/nodo
ProtectHome=yes
PrivateTmp=yes
```

Code/installer work:

* `install.sh`: `useradd --system --home /nodo --shell /usr/sbin/nologin nodo`;
  `chown -R nodo:nodo /nodo/storage` (and migrate existing installs);
  `/etc/sysctl.d/60-nodo.conf` with `net.ipv4.ip_forward=1`; create the guest
  bridge once (or let the daemon do it — it has the cap).
* `cgroups.py`: when `CGROUPS_BASE_DIR` is unset, use the daemon's own cgroup
  (`/sys/fs/cgroup` + `/proc/self/cgroup`), move the daemon into a
  `supervisor/` leaf at startup, then create `nodo-ch/` beside it — the exact
  sequence verified in §2.4. `src/manager/energy/monitor.py:178` reads the same
  key and must follow.
* virtiofsd: default `--sandbox namespace` (row 12); add `uidmap` + a subuid range
  for `nodo` (the packer's installer already does this for the operator).
* `nodo observe` capture: either `AmbientCapabilities=… CAP_NET_RAW` in the
  daemon and capture there, or leave capture as a `sudo` feature.

**Caveats checked against the code:**

* Ambient capabilities are inherited by **every** exec'd child. That includes
  CH, virtiofsd, `ip`, `nft` — intended — but also anything else the daemon
  spawns. If the daemon ever starts the rootless BuildKit packer itself,
  `NoNewPrivileges=yes` must stay **off** (rootlesskit relies on setuid
  `newuidmap`), which is why it is not in the unit above.
* The reachability probe (row 9) needs `CAP_SYS_ADMIN`; the daemon must report
  "not proven" rather than fail startup. `doctor` keeps proving it as root.
* `ProtectSystem=strict` will surface every write outside `ReadWritePaths` —
  a feature, but expect a round of fixes.

**Operator after Phase 2:** same as Phase 1; the difference is what a compromised
daemon can do.

### Phase 3 — no capabilities in the daemon (M–L, optional)

Move rows 4–8 into **`nodo-netd`** (§4 C): a few hundred lines, its own unit
with `CAP_NET_ADMIN` only, socket-activated, accepting requests only from uid
`nodo`, writing only into nodo's own nft table and only `tap<sha1[:10]>` /
`nodo-br-ch` links. The daemon, CH, QEMU and virtiofsd then run with **no
capabilities**; §2.3 is the proof that CH works that way. The existing
`src/virtualizers/microvm/firewall.py` (regex-validated arguments, mandatory
audit comments) is most of the helper's validation already.

### Phase 4 — fully unprivileged networking (L, long-term)

User-mode networking (§4 E) for hosts where even a helper is unacceptable,
behind the existing `NETWORK_MODE` switch (`network.py:163-166` rejects
everything but `tap_bridge` today). Lower throughput; the guest allow-list moves
out of nftables. Not recommended before Phases 1–2 ship.

---

## 6. Security, honestly

* **Today:** the daemon is root, `/nodo` is owned by the invoking login user
  (`install.sh:468`), and `config.yaml` is world-writable (`install.sh:245`). A
  root process running code and configuration that non-root accounts can modify
  means the privilege boundary is largely nominal already. Phase 0 item 2 and the
  ownership change in Phase 2 fix that regardless of the rest.
* **`CAP_NET_ADMIN` is still powerful.** Within the host network namespace it can
  flush or rewrite every firewall rule (not just nodo's), add routes, change
  `net.*` sysctls (§2.1), reconfigure or down any interface, and mirror traffic
  with `tc`. It cannot read other users' files, load modules, ptrace, or mount.
  Phase 2 turns "a daemon bug is host root" into "a daemon bug is host network
  admin" — a real reduction, not elimination.
* **Phase 3** confines that power to a component small enough to audit, with a
  fixed request vocabulary.
* **Tap ownership matters** (§2.3): always create taps with `user nodo`; an
  ownerless persistent tap can be attached by any local user.
* **`kvm` group** grants KVM access only; it is not root-equivalent (unlike the
  `docker` group, which this plan deliberately does not use).

---

## 7. WSL specifics

* The WSL rootfs logs in as **root by default** (`bash/setup_linux_x86.sh:477-478`,
  `bash/install.ps1:1178-1179`), so WSL operators do not type `sudo` today; the
  complaint is a native-Linux one. The Phase 1 CLI changes still matter there for
  agents and scripts that run as a normal user.
* WSL's systemd (enabled by the installer) supports `User=`,
  `AmbientCapabilities=` and `Delegate=` the same way — **verified** on WSL2
  (§2.5): the transient unit got a user-owned delegated cgroup and set per-VM
  limits. Polkit was not tested.
* `/dev/kvm` ownership/mode under WSL2 varies with the kernel and udev rules;
  `nodo doctor` already checks access for a non-root service user
  (`doctor.py:113-131`) and suggests `usermod -aG kvm`.
* The distro is single-tenant behind Hyper-V, so the security gain of Phases 2–3
  is smaller on WSL than on a shared Linux host. A sensible order is: Phase 1
  everywhere, then Phase 2 on native Linux first.
* Phase 2 could later switch `[user] default=root` to a normal user in group
  `nodo`; not required.

---

## 8. Open questions for Josemi

1. **Polkit vs. a `Restart` RPC** for `nodo daemon restart` / `nodo config set`:
   polkit is less code; an RPC also works where polkit is absent (WSL minimal
   rootfs?). Preference?
2. **Storage location:** keep `/nodo/storage` (chown to `nodo`) or move to
   `/var/lib/nodo` (`StateDirectory=`, cleaner FHS, migration needed)?
3. **Is Phase 3 (`nodo-netd`) worth it**, or is Phase 2's ambient
   `CAP_NET_ADMIN` an acceptable end state for now?
4. **`nodo observe` capture:** daemon-side capture over RPC (needs
   `CAP_NET_RAW` in the daemon/helper), or keep it a `sudo` feature?
5. ~~Re-run §2.3 on x86_64 + KVM~~ — done on WSL2 (§2.5), all confirmed. A
   native (non-WSL) x86_64 host would still be worth one run before Phase 2
   merges; the probe script is ~150 lines and can be attached.
6. Should ROOTLESS.md's Route A step 3 be corrected in place (§2.1)? This PR only
   points to it.
