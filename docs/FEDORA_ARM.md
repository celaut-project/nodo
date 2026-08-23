# Fedora / RHEL and aarch64 Hosts

Nodo installs and runs on non-Debian Linux and on 64-bit ARM. This page covers what
is specific to those hosts; everything else is in [`INSTALL.md`](INSTALL.md).

Verified end to end on **Fedora Linux Asahi Remix 44** (Apple M1, kernel
`7.1.6-400.asahi.fc44.aarch64+16k`, **16 KiB pages**, SELinux `Enforcing`), which is
the most hostile combination currently known to work: a vendor kernel with no LTS
track, a non-architectural GIC, a 36-bit IPA limit, and a page size most binaries are
never linked for.

## What the installer does differently here

Nothing you need to do by hand. Listed so the behaviour is findable:

| Concern | Where it is handled |
|---|---|
| Package manager (`apt` vs `dnf` vs `zypper`) | `bash/lib_pkg.sh` — `detect_pkg_mgr`, `pkg_update`, `pkg_install`, with per-distro names in `NODO_HOST_PACKAGE_ALIASES` |
| Packages actually present afterwards | `verify_host_tools()` fails the install rather than letting a missing tool surface at first launch |
| Admin group for the systemd unit (`sudo` on Debian, `wheel` on Fedora/RHEL) | `resolve_admin_group()` fills `{{ADMIN_GROUP}}` in `bash/nodo.service.template`. `nodo doctor` resolves it the same way (`_resolve_admin_group()`), so it cannot rewrite the unit into one systemd refuses to load |
| UTF-8 locale (`locale-gen` is Debian-only) | `ensure_utf8_locale()` guards on `command -v`; on Fedora the locale comes from `glibc-langpack-en` |
| Executable architectures | The setup scripts switch off the architecture this host cannot run, so the node does not advertise work it would fail to start |
| Guest kernel format | Never taken from `/boot`. Fedora/RHEL/openSUSE ship a `CONFIG_EFI_ZBOOT` `vmlinuz` — a PE wrapping a compressed Image — which Cloud Hypervisor cannot load at all (`UefiLoad(UefiTooBig)`). The guest kernel is a release asset built by `.github/workflows/guest-kernel.yml`, which asserts the raw arm64 Image magic before publishing |
| Guest userspace and initramfs | Also release assets, so the guest does not vary with the host's busybox applet set, `cpio`, `gzip` or umask. See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) |
| Initramfs inspection | `cpio`, never `lsinitramfs` (Debian's initramfs-tools) or `lsinitrd` (dracut). The gzip'd `newc` cpio layout is a kernel ABI; only the inspectors are distro-branded |

Fedora package names worth knowing if you install by hand: `gcc make` for
`build-essential`, `procps-ng`, `iproute`, `iputils`, `glibc-langpack-en`, and
`e2fsprogs` for `debugfs`. `initramfs-tools` and `dracut` are **not** needed.

## aarch64 specifics

- **Nothing in `bash/requirements.txt` compiles here today.** The whole tree resolves
  to `cp311`/aarch64 wheels: the nine PyPI pins, plus `bee-rpc` and `ergpy` (both pure
  Python) and their transitive `grpcio==1.56.0`, `protobuf`, `JPype1` and `stubgenj`.
  `psutil` joined them at `6.1.1`; below `6.0.0` there is no aarch64 wheel and it built
  from source.

  A compiler is still installed, as insurance rather than a current need. Those wheels
  are tagged per CPython version, and **`grpcio==1.56.0` publishes aarch64 wheels only
  up to `cp311`** — the same 3.11 the portable runtime pins. Moving that runtime to
  3.12 or later turns grpcio into a from-source C++ build, so treat the two pins as
  coupled.

  When something does build from source, it looks for `clang` by name: the portable
  CPython from python-build-standalone records `CC=clang` in its `sysconfig`. The setup
  scripts fall back to `CC=gcc CXX=g++` when clang is absent.
- **The guest console is `ttyAMA0`, and it is not optional.** CH gives aarch64 guests
  a PL011; `nodo-guest-arm64.config` enables that driver and no 8250 at all. Naming
  `ttyS0` there is not a lost log — the kernel cannot bind a device it has no driver
  for, so `/dev/console` never opens, `/init` dies on its first statement
  (`exec >/dev/console 2>&1`, and a non-interactive shell exits on a failed
  redirection), and the launch ends as `Kernel panic - not syncing: Attempted to kill
  init! exitcode=0x00000100` at ~0.1s with no output of its own.

  Both cmdline builders derive it from `src/virtualizers/ch/guest.py`, so it cannot
  be set wrong. If `virtualizers.ch.KERNEL_CMDLINE_EXTRA` in an older `config.yaml`
  still carries `console=ttyS0` — it was the shipped default — it is dropped with a
  warning rather than honoured.
- **This host executes `linux/amd64` services under emulation, not KVM.** The
  installer provisions the amd64 guest assets and installs `qemu-system-x86`, so an
  ARM node does take work from a catalogue that is mostly `linux/amd64` — but under
  TCG, an order of magnitude slower than the arm64 services it boots natively. Set
  `virtualizers.qemu.ENABLE: false` to serve only arm64. *Packing* is still
  host-only (see *Operational notes* in [`INSTALL.md`](INSTALL.md)).

## Verified on 16 KiB pages

Worth recording, because these were the real risks rather than anything in the
installer:

- **Cloud Hypervisor v51.1 boots guests** despite Apple's non-architectural vGIC, the
  `IPA Size Limit: 36 bits`, and the host's 16 KiB pages. The full path works: kernel
  → `virtio_blk` sees `/dev/vda` → `/init` mounts the ext4 → `switch_root`.
- **The portable CPython 3.11 and Temurin JRE 21 both run.** This was the risk that
  mattered: a binary linked with `max-page-size=4096` cannot be mapped on a 16 KiB
  kernel. Neither is.
- **The initramfs is byte-reproducible across distros.** The published
  `initramfs-linux-arm64` was rebuilt here from the pinned busybox and matched the
  Ubuntu 24.04 CI runner's build exactly, so the digests in
  `bash/guest-kernel/SHA256SUMS.pinned` are re-derivable rather than merely trusted:

  ```bash
  gh release download guest-kernel --pattern busybox-linux-arm64 --dir /tmp/gk
  bash bash/guest-kernel/build-initramfs.sh arm64 /tmp/gk /tmp/gk/busybox-linux-arm64
  ```

- **`execute`'s preflight** (`ip`, `sysctl`, `iptables`, `debugfs`, `ping`) is
  satisfied by Fedora's packages. `iptables` is the nft backend and accepts the legacy
  syntax `src/virtualizers/ch/firewall.py` uses.
- **SELinux in `Enforcing`** does not prevent `nodo.service` from starting out of
  `/nodo`.
- **Nothing in `src/` is Debian-specific** — no `apt`, `dpkg` or `lsb_release`.

## Kernels newer than Cloud Hypervisor

`nodo doctor` prints an `[INFO]` note for host kernels newer than the ones CH is most
tested against, and defers to its own KVM smoke test for the verdict. That note is not
a diagnosis: CH v51.1 runs guests fine under 7.1.6 here. Where a kernel genuinely is
the problem, guests fail with `VcpuRun InternalError`, and the fix is to upgrade Cloud
Hypervisor — an LTS kernel is only an option on platforms that offer one, which
Apple Silicon under Asahi does not.
