# Troubleshooting

Common first-run failures and how to read them. For the diagnostic command
itself, see `nodo doctor` in [`USAGE.md`](USAGE.md); for packing errors see the
*Common Issues and Solutions* section of [`PACKING.md`](PACKING.md).

## Architecture mismatch (most common packing failure)

**Symptom:** `nodo pack` fails early with an explicit
architecture/unsupported-architecture message.

**Why:** **packing** is host-only. A local build runs the target's own toolchain
and nodo installs no binfmt handler, so a service's `service.json → architecture`
(e.g. `linux/amd64`, `linux/arm64`) must match the host running the packer
(`src/utils/arch_guard.py`).

Note this is a *packing* limit, not an execution one: a default install
**executes both architectures** — the host's under Cloud Hypervisor/KVM and the
other under QEMU emulation (see below).

**Fix:**
- Set `service.json → architecture` to your host's architecture (`uname -m` →
  `x86_64` = `linux/amd64`, `aarch64` = `linux/arm64`). See
  [`PACKING.md`](PACKING.md) → *architecture*.
- Or pack on a host of the target architecture.
- `packer.ARM_PACKER_SUPPORT` / `packer.X86_PACKER_SUPPORT` in `config.yaml`
  control which architectures the packer will accept/announce.

## Unsupported architecture when *executing* a service

**Symptom:** a launch fails with `UnsupportedArchitectureException`, or — on an
older config — deep inside the build with
`Cloud Hypervisor kernel not found at '.../linux/arm64/vmlinuz'`.

**Why:** the node executes its **own** architecture under Cloud Hypervisor, plus
any **foreign** one QEMU can emulate. That second half needs three things
present, all of which a default install provides:

1. `virtualizers.qemu.ENABLE: true` in `config.yaml` (the default);
2. the emulator, `qemu-system-aarch64` / `qemu-system-x86_64`, on `PATH` (or set
   in `virtualizers.qemu.BINARY_PATHS`);
3. that arch's guest kernel **and** initramfs on disk, at the paths in
   `virtualizers.ch.KERNEL_PATHS` / `INITRAMFS_PATHS`.

With any one missing, the node simply does not advertise that architecture and a
request for it is refused up front — it never accepts work it cannot boot.

**Fix:** check all three, in that order:

```bash
grep -A3 -e ENABLE -e KERNEL_PATHS -e INITRAMFS_PATHS /nodo/config.yaml
command -v qemu-system-aarch64 qemu-system-x86_64
ls -l /nodo/cloud_hypervisor/kernels/linux/*/vmlinuz /nodo/cloud_hypervisor/initramfs/linux/*/initramfs
```

Missing guest assets are release artifacts: re-running the installer downloads
both architectures. A missing emulator is a distro package — `qemu-system-arm`
(Debian/Ubuntu) or `qemu-system-aarch64` (Fedora) for arm64 guests,
`qemu-system-x86` for amd64 ones; the installer tries to install it but does not
fail the install if it cannot.

If the config still carries `builder.ARM_SUPPORT` or `builder.X86_SUPPORT`, the
node refuses to start and names them: those keys are gone, delete them. They used
to *declare* which architectures the node executed, which is what produced the
missing-kernel crash above — a host announced arm64 and then had no arm64 kernel
to boot. Capability is now derived from what is installed.

Emulated execution is an order of magnitude slower than KVM. To serve only the
host's architecture, set `virtualizers.qemu.ENABLE: false`.

## KVM unavailable (services won't execute)

**Symptom:** `nodo execute` fails to launch a microVM; `nodo doctor` reports no
`/dev/kvm`, missing KVM modules, or a failed KVM smoke test.

**Why:** services run as Cloud Hypervisor microVMs, which need hardware
virtualization (`/dev/kvm`). It is commonly unavailable when:
- CPU virtualization (VT-x/`vmx`, AMD-V/`svm`) is disabled in BIOS/UEFI.
- Running inside a VM without **nested virtualization** enabled.
- Running inside an unprivileged container without `/dev/kvm` passed through.
- On WSL2 — see [`WSL.md`](WSL.md) for the mirrored-networking / KVM setup.
- On Fedora/RHEL or aarch64 — see [`FEDORA_ARM.md`](FEDORA_ARM.md), which also covers
  why `doctor` only notes (rather than faults) a kernel newer than Cloud Hypervisor.

**Fix — run `sudo nodo doctor` and read its checks:**
- **CPU virtualization flags (`vmx`/`svm`)** — if absent, enable virtualization in
  firmware, or enable nested virt on the parent hypervisor.
- **KVM modules and `/dev/kvm` access** — load `kvm` + `kvm_intel`/`kvm_amd`;
  ensure the nodo user can access `/dev/kvm`.
- **Cloud Hypervisor binary existence and version** — reinstall CH assets
  ([`INSTALL.md`](INSTALL.md) step 10) if missing.
- **Guest kernel (`vmlinuz`) / initramfs presence** — the guest kernel must
  exist and be ≥1 MiB; the initramfs must exist and contain `init`,
  `bin/busybox`, and the `etc/nodo-ch-initramfs.marker` marker (verified with
  `cpio`). Both are release assets, so re-running the installer downloads them
  again. To audit the installed initramfs instead, rebuild it from this checkout
  — `bash/build_ch_initramfs.sh` is byte-reproducible, so the digests must match:
  `bash /nodo/bash/build_ch_initramfs.sh /nodo linux/arm64 /tmp/initramfs`
  (`linux/amd64` on x86_64), then compare `sha256sum` with the installed file.
- **KVM smoke test** — launches a minimal VM; a failure here means CH cannot
  execute vCPUs on this kernel even though the earlier checks passed (often a
  container/nested-virt limitation, or a bleeding-edge host kernel that `doctor`
  warns about).

`doctor` also checks/repairs the `nodo.service` systemd unit; run it with `sudo`.

## Guest kernel missing or rejected by Cloud Hypervisor

**Symptom:** the installer fails downloading the guest kernel, initramfs or
busybox, or CH refuses to boot the kernel (`KernelLoad(Pe(...))`, `UefiLoad(UefiTooBig)`).

**Why:** the guest kernel is a Nodo release asset (`vmlinuz-linux-arm64` /
`vmlinuz-linux-amd64` under the `guest-kernel-vN` tag), built by
`.github/workflows/guest-kernel.yml`. It is deliberately **not** the host's
`/boot` kernel: distro kernels differ in size and format, and Fedora/RHEL ship a
`CONFIG_EFI_ZBOOT` image that CH's PE loader cannot read at all.

**Fix:**
- Re-run the installer, or download the asset by hand into
  `virtualizers.ch.KERNEL_PATHS[<arch>]` and verify it against
  `bash/guest-kernel/SHA256SUMS.pinned` in this checkout — not against the
  `SHA256SUMS` published in the release, which sits in the same mutable place as the
  artifact it vouches for.
- To pin a different kernel release, change `GUEST_KERNEL_VERSION` in `install.sh`
  **and** update `TAG` plus all six digests (kernel, initramfs and busybox, per arch)
  in `bash/guest-kernel/SHA256SUMS.pinned`; the installer refuses to run when the two
  disagree. Cross-check the digests against the CI artifacts of the run that built
  them (`gh run download <run-id>`), which is a copy independent of the release.
- To build them locally: `bash bash/guest-kernel/build.sh <arm64|x86_64> <out-dir>`
  (native builds only; needs ~15 GB of scratch space in `TMPDIR`) and
  `bash bash/guest-kernel/build-busybox.sh <arm64|x86_64> <out-dir>`. The
  initramfs is `bash bash/build_ch_initramfs.sh <MAIN_DIR> <arch-tag> <out-file>`,
  which needs the provisioned busybox and reproduces the published image byte for
  byte.
- The same release carries `busybox-linux-{arm64,amd64}`, the static binary that is
  the guest's entire userspace, and `initramfs-linux-{arm64,amd64}`, the image CI
  builds from it with `bash/build_ch_initramfs.sh`. busybox is installed at
  `<MAIN_DIR>/cloud_hypervisor/busybox/<arch>/busybox`, which is the only source
  the builder accepts; it falls back to the host's busybox only when
  `NODO_ALLOW_HOST_BUSYBOX=1` is set, which the installer never does and which
  produces a dev-only image no node runs.

## Initramfs contract version mismatch

**Symptom:** `nodo execute` fails before the microVM starts, with *Cloud
Hypervisor initramfs speaks contract version '…', but this node needs '…'*.

**Why:** the initramfs' `/init` and `src/virtualizers/ch/execute.py` share a
contract — which files `execute.py` writes into the service rootfs
(`__config__`, `.__nodo_entrypoint`, `.__nodo_virtiofs`) and how `/init` reads
them. The initramfs is a pinned release asset while the contract lives in this
checkout, so the two can be bumped out of step. `execute.py` compares its
`INITRAMFS_CONTRACT_VERSION` against `etc/nodo-ch-initramfs.marker` inside the
archive and refuses the launch, instead of letting the guest boot and park in
`/init`'s fatal loop until the launch times out with nothing to show.

**Fix:** re-run the installer to fetch the initramfs matching this code, or bump
`GUEST_KERNEL_VERSION` (and `bash/guest-kernel/SHA256SUMS.pinned`) to the release
whose initramfs speaks the version this checkout expects.

## Gateway port unreachable, or never assigned

**Symptom, one of:**
- The node refuses to start with *"network.GATEWAY_PORT is not assigned"*, or
  *"Gateway port N could not be verified as reachable"*.
- The node runs, but another machine on the LAN gets **"no route to host"**
  connecting to its port, and peers never reach it.

**Why:** nodo writes an accept rule for the port in its own nftables table, and on
a host that shares the firewall that rule is *necessary but not sufficient*. In
nftables `accept` ends the evaluation of its own chain only; the packet still
traverses every other base chain on the same hook, and a `reject` there wins
regardless of priority. firewalld's default zone ends in
`reject with icmpx admin-prohibited` — which is precisely the "no route to host"
above.

So nodo proves the port before it serves on it. At every daemon start it brings up
the guest bridge, connects to the port from inside that subnet the way a guest
would, and **refuses to start** if that connect is conclusively rejected — taking
its own accept rule back out on the way, since nothing is going to answer there.
That is deliberate: a node that will not start is a loud failure, an unreachable one
is a node that looks healthy and answers nothing.

**What nodo tells you.** The refusal reports what it found on the input hook
outside nodo's own tables — the chains that can reject, *or* that nothing can, *or*
that the ruleset could not be read at all, which is a different thing and now says
so. Then, when it can, the single command that fixes it. nodo checks which
high-level firewall is *running* on the host — firewalld, ufw — and prints its
command, e.g.:

> Rejecting chains, outside nodo's own ruleset:
>   - inet firewalld / filter_INPUT (hook input, priority 10): contains a reject rule
> This host runs firewalld. Open the port with:
>   sudo firewall-cmd --permanent --add-port=58443/tcp && sudo firewall-cmd --reload

Where no front-end is running there is nothing to name — the ruleset may be a
hand-written `nft` file or a config-management template — so it states the property
instead: inbound TCP on that port must be accepted on the netfilter input hook, and
no other base chain on that hook may reject or drop it. nodo never invents a command
for a front-end it did not detect.

**The port it asks about does not change between runs.** It is written to
`network.GATEWAY_PORT` as soon as it is opened in nodo's own ruleset, before it is
proven, precisely so that "open TCP 58443" is still true on the next start. Earlier
versions withheld the port until it was proven and therefore asked about a fresh
random one every run — an operator following the instructions was always a step
behind.

**And it is only proven once.** A successful check is recorded in
`<main.CACHE>/gateway_port_passed`, so a restart does not rebuild a network
namespace to re-answer it. Editing `network.GATEWAY_PORT` deletes that file (the
TUI does it too), and so does a reboot: if you opened the port with
`firewall-cmd --add-port` and no `--permanent`, the rule is gone after the reboot
and the check has to run again. Delete the file yourself to force a re-check.

**The alert is printed last, on purpose.** It is written while the config loads —
during nodo's imports on a fresh install — so it is held back to the end of the
process and left in `.gateway_notice` beside `config.yaml`; `install.sh` prints that
file as its final act. In a terminal the last thing printed is the first thing read.

**"Not assigned" after an install.** The port is assigned by `install.sh` and by
`sudo nodo serve`, and by nothing else — an ordinary `nodo` command will not do it
for you, by design: picking a port writes a rule into the host's firewall. If the
installer could not assign one it says so and points here, and the alert is the last
thing it prints.

**Fix — run `sudo nodo doctor` and read the gateway section.** It reports the result
of a real connect from the guest bridge (it supplies its own listener, so this works
with the node stopped), the foreign chains that can reject, and the same command.
Then start the node again. [`FIREWALL.md`](FIREWALL.md) → *Sharing the host with
another firewall* has the longer worked examples, including giving the guest bridge
a firewalld zone of its own rather than `trusted`.

**Pinning a port you have already opened yourself** in `config.yaml`
(`network.GATEWAY_PORT: 58443`) stops nodo re-picking — but it does not exempt the
port from the check. A pinned port is verified at the next start like any other, and
the node refuses to serve if it is rejected. That is the point: a hand-pinned port
used to be the one path with no verification on it, which is how a node ends up
serving on a port firewalld rejects.

**Reachability from outside the LAN is a separate problem.** Nothing on the host
can prove it: a connect from inside succeeds whether or not the router forwards
anything. If peers on the Internet must reach this node you also need a port
forward to this host's `GATEWAY_PORT`, an ISP that is not putting you behind CGNAT,
and a check from a genuinely external network. Run `nodo nat-guide`, which reports
the facts it can establish and tells you the rest.

## KyA prompt blocks automation

**Symptom:** a headless/agent run hangs or exits at the Know Your Assumptions
`yes/no` prompt.

**Fix:** pre-accept by creating the marker file (only in environments you
control) — `mkdir -p /nodo/storage && touch /nodo/storage/.acceptedkya`. See
[`USAGE.md`](USAGE.md#non-interactive-use-automation--agents-️) and
[`KyA.md`](KyA.md).

## Payment / reputation features error out

**Symptom:** `nodo info` or payment/reputation actions report a Java dependency
missing.

**Why:** Java is **optional** and only required for Ergo-backed payment and
reputation features ([`INSTALL.md`](INSTALL.md)). Core install/pack/execute work
without it.

**Fix:** install the local JRE (INSTALL.md step 7), or avoid the payment/
reputation commands. Also ensure `ledgers.ergo.WALLET_MNEMONIC` is set if you
intend to pay or submit reputation (see [`CONFIG.md`](CONFIG.md)).

## `nodo pack` can't find a packer

**Symptom:** default-mode pack fails to resolve a packer-service.

**Why/Fix:** in default mode (`packer.local: false`) you must set the packer's
published id under `core_services` in `config.yaml` and have a running instance
(`nodo execute <packer-service id>`). Alternatively set
`packer.PACKER_SERVICE_URL` to an out-of-band packer, or set `packer.local: true`
to build locally. See [`CONFIG.md`](CONFIG.md) and [`INSTALL.md`](INSTALL.md)
step 9.

## Uninstalling

There is a full uninstall path — automatic (`uninstall.sh`) and manual. See
[`UNINSTALL.md`](UNINSTALL.md).
