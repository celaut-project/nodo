# Rootless operation: what still needs privileges, and why

An audit of every privileged operation nodo performs, and an assessment of how
much work it would take to run without `sudo`. For the diagnostic command see
`nodo doctor` in [`USAGE.md`](USAGE.md); for the privileged install steps see
[`INSTALL.md`](INSTALL.md).

> **Audited against** `stable` @ `043d00a7` on 2026-08-02. Empirical checks were
> run on Ubuntu 24.04.4 LTS, x86_64, 16 threads, cgroup v2, as a non-root user
> in group `sudo`. Results marked *verified* were measured on that host; results
> marked *expected* were read from the source but not executed.

## Summary

**The remaining `sudo` requirement has nothing to do with containers.** They are
entirely absent from the execution path — services run as Cloud Hypervisor
microVMs. What is left is, in substance, **one thing: networking**
(`CAP_NET_ADMIN`), plus a trivial cgroups path and a set of `geteuid()` guards
inherited from the Docker era.

> **Update (2026-08-21): the local packer is now rootless.** When this was first
> audited, `packer.local: true` drove an isolated `dockerd` started through sudo.
> Because an unprivileged pack could not signal a root daemon, a failed build left
> it alive holding its data-root lock while the stop script deleted its socket and
> reported success — after which every later pack died with *"already running but
> is not responding"*. The packer's only use of Docker was
> `docker buildx build --output type=tar`, and buildx is just a front end for
> BuildKit, so nodo now drives BuildKit directly under `rootlesskit`. The builder
> runs as the invoking user and `nodo pack` performs **no privileged call at all**;
> `bash/install_buildkit.sh` may ask for sudo once to provision the host
> prerequisites (`uidmap`, `rootlesskit`, subordinate id ranges). See
> *Rootless local packer* below.

The hard parts of running a hypervisor — KVM access and root filesystem image
construction — **already work unprivileged today**.

## Already unprivileged (verified)

| Requirement | Status |
|---|---|
| `/dev/kvm` | Readable/writable by the desktop user via a logind ACL (`crw-rw----+`), with no `kvm` group membership. Cloud Hypervisor itself does not need root. |
| rootfs image build | `mkfs.ext4 -d` (`src/virtualizers/ch/build.py:851`) plus `debugfs` writes (`src/virtualizers/ch/execute.py:477`) — **no `mount`, no loop devices**. This was evidently a deliberate design choice. |
| cgroup v2 | `user@1000.service` already has `cpu memory pids` delegated **and writable** in `cgroup.subtree_control`. |
| Exposed ports | `network.FREE_PORTS_RANGE` defaults to 50000–60000 — unprivileged. |
| `nodo observe` | Already degrades gracefully without `CAP_NET_RAW`: it loses only the AF_PACKET `capture.pcap`, not the metrics (`src/commands/observe.py:748`). |

## Rootless local packer (done)

`packer.local: true` builds with BuildKit under `rootlesskit`, started and stopped
by `bash/start_buildkit_daemon.sh` / `stop_buildkit_daemon.sh`. Neither script
contains a privileged call, and `tests/test_rootless_builder.py` fails the build
if one is reintroduced.

What the host must provide once (`bash/install_buildkit.sh` provisions it):

| Prerequisite | Why | Provisioned by |
|---|---|---|
| `newuidmap` / `newgidmap` | Map more than one uid inside the namespace. Without them most Dockerfiles fail on `apt-get install`/`chown`. | `uidmap` (deb) / `shadow-utils` (rpm) / `shadow` (Arch, SUSE) |
| `/etc/subuid`, `/etc/subgid` entry | The id range those tools hand out. | `usermod --add-subuids 100000-165535` |
| `rootlesskit` | Creates the namespace. | distro package, else the upstream static binary |

**AppArmor, and why the path of the binary matters.** On Ubuntu 24.04+,
`kernel.apparmor_restrict_unprivileged_userns=1` denies user namespaces to
*unconfined* binaries — verified, `unshare -Ur` fails exactly as this document
already reported for Route B. What was **not** previously noted is that Ubuntu
ships `/etc/apparmor.d/rootlesskit`, a `flags=(unconfined)` profile granting
`userns` **by executable path** (`/usr/bin/rootlesskit`). Verified: invoking
`/usr/bin/rootlesskit` clears the restriction, while `aa-exec -p
unprivileged_userns -- unshare -Ur` does not. So nodo deliberately prefers a
distro-packaged rootlesskit over a copy under `MAIN_DIR/bin`, which the profile
would not cover; when it has to fall back to its own binary, the installer writes
a matching profile for that path.

**Consequence for Route B.** The AppArmor obstacle recorded below is real for a
bare `unshare`, but it is **not** the dead end it appeared to be: a binary with a
`userns` profile passes, and installing such a profile is a one-time privileged
step of the same kind Route A already needs. Route B's remaining cost is the
port-forwarding rewrite, not AppArmor.

## Still requires privileges

### 1. Networking — the real blocker

Roughly 30 privileged calls, 26 of them in a single file
(`src/virtualizers/ch/execute.py`):

- `_network_preflight()` (`:306`) — creates the `nodo-br-ch` bridge, assigns its
  address, brings it up, and runs `sysctl -w net.ipv4.ip_forward=1`.
- `_create_tap()` (`:381`) — `ip tuntap add` plus attaching the tap to the
  bridge, once per microVM.
- `_ensure_masquerade()` (`:334`) and `_add_dnat_rule()` (`:503`) —
  MASQUERADE, DNAT/PREROUTING and FORWARD rules per instance.

Plus `src/virtualizers/ch/firewall.py`, `src/virtualizers/firewall.py:121`, and
`src/utils/config.py:115`, which opens the gateway port in the `INPUT` chain.

Verified failures as a non-root user:

```
ip tuntap add dev tapPROBE mode tap   → ioctl(TUNSETIFF): Operation not permitted
ip link add brPROBE type bridge       → RTNETLINK answers: Operation not permitted
iptables -t nat -L POSTROUTING -n     → Permission denied (you must be root)
sysctl -w net.ipv4.ip_forward=1       → permission denied on key
```

All of these need **`CAP_NET_ADMIN`, not full root**. Note that `_run()`
(`execute.py:86`) invokes `ip`/`iptables` **without `sudo`** — it assumes the
process is already root, which is exactly what
`bash/nodo.service.template:7` (`User=root`) provides.

`/dev/net/tun` is mode `0666`, so opening the device is not the obstacle;
`TUNSETIFF` on a named interface in the initial network namespace is.

### 2. cgroups — trivial

`CGROUPS_BASE_DIR` (`src/virtualizers/ch/cgroups.py:10`) points at
`/sys/fs/cgroup`, whose root is owned by root. This is **a configuration value**,
and the user-delegated path already works (see above). Near-zero cost to change.

### 3. Installation — genuinely privileged, but relocatable

`install.sh` and `bash/setup_linux_x86.sh` need root for:

- `apt-get install` of build dependencies (the guest kernel is downloaded from
  the `guest-kernel-vN` release and needs no package);
- the unit file at `/etc/systemd/system/nodo.service` plus
  `systemctl enable/start`;
- the wrapper script at `/usr/local/bin/nodo`;
- `/nodo` as the installation root.

The last three are relocatable: `--target-dir ~/.local/nodo`, a **systemd user
unit** under `~/.config/systemd/user/`, and a wrapper in `~/.local/bin`. Only the
apt packages genuinely need root, and only once.

## Inherited `geteuid()` guards from the Docker era

`kill` (`src/commands/kill.py:7`), `remove` (`src/commands/remove.py:16`) and
`prune_containers` (`nodo.py:636`) still require root. The fourth,
`local_docker_packer`, is **gone**: it is now `local_builder` and carries no
privilege check, because the builder it queries runs as the invoking user. There
is a **real design inconsistency** in what remains:

- `nodo execute` reaches the daemon over **gRPC** (`src/commands/execute.py`, via
  `get_execute_client`) and therefore needs no privileges at all;
- `nodo kill` calls `stop_instance()` **in-process** (`src/manager/manager.py:486`)
  and so touches taps and iptables rules directly.

Making `kill` and `remove` delegate to the daemon over gRPC — the way `execute`
already does — removes two `sudo` requirements from the CLI **without changing
the privilege model at all**. `remove` only manipulates registry files on disk;
its guard is pure Docker-era legacy.

`daemon`, `doctor` and `update` do need real root: system-level `systemctl` and
re-running the installer respectively.

## Three routes, by effort

### A. `AmbientCapabilities` — low effort, high payoff

Run the daemon as a normal user with `CAP_NET_ADMIN` (plus `CAP_NET_RAW` for
`observe`) and a restricted `CapabilityBoundingSet`. The networking logic needs
no changes whatsoever. Required work:

1. Point `CGROUPS_BASE_DIR` at the user-delegated cgroup path.
2. Drop the inherited `geteuid()` guards (see above).
3. **Mandatory fix:** `sysctl -w net.ipv4.ip_forward=1` would still fail, because
   `CAP_NET_ADMIN` does **not** bypass the DAC permissions on `/proc/sys/...`
   (root:root, mode 0644). Move it to `/etc/sysctl.d/` at install time and only
   *verify* it at runtime.

Outcome: root disappears from the runtime entirely, and remains only as a
one-time install step.

### B. netns + userns — genuinely rootless, high effort

`NETWORK_MODE` (`execute.py:52`) already exists as an extension point and
explicitly rejects anything but `tap_bridge` ("this phase supports only
'tap_bridge'"), so the architecture anticipated this. The work is replacing
bridge + DNAT with `pasta`/`slirp4netns` and their own port forwarding.

**Obstacle on Ubuntu 24.04, since qualified:** `kernel.apparmor_restrict_unprivileged_userns = 1`
blocks unprivileged user namespaces — a test namespace could not be created
(`unshare: write failed /proc/self/uid_map: Operation not permitted`). This route
needs an AppArmor profile granting `userns` to the binary that creates the
namespace. That is a one-time privileged install step rather than a blocker: the
rootless packer already relies on exactly such a profile (see *Rootless local
packer* above), so the pattern is proven on this host.

### C. Minimal privileged helper — medium effort

A small service that only creates taps and applies rules from fixed templates,
concentrating the attack surface in a small auditable component.
`src/virtualizers/ch/firewall.py:14` already validates its arguments with a
regex and mandates audit comments, so the necessary discipline is in place.

## Recommendation

**Route A**, staged in this order, each step independently verifiable with
`nodo doctor`:

1. The `geteuid()` guards, with `kill`/`remove` delegating over gRPC —
   independent of everything else, and removes `sudo` from everyday use.
2. `CGROUPS_BASE_DIR`.
3. `ip_forward` moved to `sysctl.d`.
4. systemd user unit plus ambient capabilities.

Route B is the correct long-term architectural target, but today it runs into
AppArmor on Ubuntu 24.04 and a port-forwarding rewrite; it should not be started
before A works.

## Open questions

- `cloud-hypervisor` was not installed on the audit host, so it was **not**
  verified that the v51.1 binary starts with only ambient `CAP_NET_ADMIN` and a
  pre-existing tap. Likely, but worth confirming.
- It was not measured whether `_wait_guest_network_ready()` (`execute.py:1170`)
  depends on a privileged `ping`.

## Documentation discrepancy

`docs/skill/SKILL.md` describes [`INSTALL.md`](INSTALL.md) as offering a
"no-sudo path", but `INSTALL.md:18` states "You have `sudo` access" and uses
`sudo` throughout. That description is currently incorrect.
