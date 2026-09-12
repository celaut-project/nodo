# Releasing Nodo WSL (Windows)

## Overview

The Windows installer (`bash/install.ps1` / `Nodo-Setup.exe`) downloads its assets from
a **single floating release tag, `wsl-exe`**. All four URLs in `install.ps1` point at it:

```
https://github.com/celaut-project/nodo/releases/download/wsl-exe/{bzImage,debian.tar,vmlinuz,initramfs}
```

So a release is shipped by **replacing the assets on the `wsl-exe` release**, not by
cutting a new tag. Publishing under a fresh tag (`v2`, `v3`, …) leaves every installer
in the wild downloading the old assets, because nothing points at the new tag.

> The `wsl-exe` assets currently date from **2026-07-17**. Anything merged since then
> reaches a Windows node only through the `stable` branch, which `install.sh` is pulled
> from at install time — not through the rootfs.

## Release assets

| File | Purpose | Notes |
|---|---|---|
| `debian.tar` | WSL2 rootfs imported by the installer | Ubuntu 22.04 base with nodo code + venv pre-installed |
| `vmlinuz` | Cloud Hypervisor **guest** kernel | Downloaded by `setup_linux_x86.sh` from the `guest-kernel-vN` release |
| `bzImage` | WSL2 **host** kernel | Written to `C:\wsl-kernel\bzImage` and referenced in `.wslconfig`; currently Josemi's custom `microhobby` build — requires a separate kernel build environment to update |
| `initramfs` | WSL2 distro-side initramfs, paired with `bzImage` | `install.ps1` (STEP 5.3, "Downloading internal kernel") downloads it from the `wsl-exe` release to `/boot/initramfs` inside the distro, before `install.sh` runs. **Unrelated to the Cloud Hypervisor guest initramfs**, which `setup_linux_x86.sh` downloads from the `guest-kernel-vN` release into `$TARGET_DIR/cloud_hypervisor/initramfs/<arch>/initramfs` — nothing writes to `/boot` at install time |

## When to cut a release

- Any change to `bash/build_ch_initramfs.sh` (the guest userspace it assembles, or
  the `/init` ↔ `execute.py` contract version it stamps) — note the image itself is
  built by CI and shipped in the `guest-kernel-vN` release, so that pin moves first
- Any change to `bash/setup_linux_x86.sh` (setup flow, dependency versions)
- Significant feature merges that should ship to Windows users

Re-uploading the assets is the whole ship step; see "Publish the assets" below.

## Build prerequisites

- Linux x86_64 host with Docker (Alienware WSL Ubuntu2204 recommended — Docker 24+ present)
- `gh` CLI authenticated with write access to `celaut-project/nodo`
- ~10 GB free disk space (the exported Ubuntu rootfs is ~3–4 GB compressed)

## Build process

### 1. Clone dev and start a privileged Ubuntu 22.04 container

```bash
cd /tmp
rm -rf nodo-release-build
git clone https://github.com/celaut-project/nodo.git --branch dev nodo-release-build

# Start a privileged container. Nothing in setup needs modprobe any more — the
# initramfs is a release asset, not built here — but this recipe has only been
# validated with --privileged.
docker run --privileged -d --name nodo-v2-build ubuntu:22.04 sleep infinity

# Copy nodo code into the container filesystem (NOT a bind mount — must be inside for docker export)
docker cp /tmp/nodo-release-build/. nodo-v2-build:/nodo/
```

### 2. Run setup

`setup_linux_x86.sh` downloads the guest kernel, initramfs and busybox from the `guest-kernel-vN`
release, so the build container needs no kernel package of its own and builds no part of the guest
(`/boot` is never read, and there are no guest modules — `CONFIG_MODULES` is off in the guest
kernel).

```bash
# Copy the example config so setup_linux_x86.sh can find config.yaml
docker exec nodo-v2-build cp /nodo/config.example.yaml /nodo/config.yaml

# Run the full setup — installs portable Python, Cloud Hypervisor binary,
# downloads the guest kernel/initramfs/busybox, creates venv, runs migrations
docker exec nodo-v2-build bash /nodo/bash/setup_linux_x86.sh /nodo
```


### 3. Export assets

```bash
# Export full rootfs
docker export nodo-v2-build > /tmp/debian.tar

# Copy out the guest kernel + initramfs provisioned by setup_linux_x86.sh.
# vmlinuz here is the Nodo guest kernel asset (guest-kernel-vN release), which the
# setup script downloads — not the build container's distro kernel.
docker cp nodo-v2-build:/nodo/cloud_hypervisor/kernels/linux/amd64/vmlinuz /tmp/vmlinuz
docker cp nodo-v2-build:/nodo/cloud_hypervisor/initramfs/linux/amd64/initramfs /tmp/initramfs

# bzImage is the same kernel binary (some CH versions expect this name)
cp /tmp/vmlinuz /tmp/bzImage

docker rm -f nodo-v2-build
```

> **Note on `bzImage`:** The original v1 `bzImage` was a custom `microhobby` 6.16.0 WSL2 host
> kernel built by Josemi. Rebuilding it requires his kernel build environment. If you don't
> have that, copying `vmlinuz` as `bzImage` ships a stock kernel instead, which works fine
> for Cloud Hypervisor guests but changes the WSL2 host kernel for installer users.
> To keep the custom host kernel, download v1's `bzImage` and include it unchanged.

### 4. Publish the assets

Upload onto the existing `wsl-exe` release, replacing what is there. `--clobber` is the
point: the tag does not move, the files behind it do.

```bash
gh release upload wsl-exe \
  --repo celaut-project/nodo \
  --clobber \
  /tmp/debian.tar /tmp/vmlinuz /tmp/bzImage /tmp/initramfs
```

Verify the assets took, and that nothing still points elsewhere:

```bash
gh release view wsl-exe --repo celaut-project/nodo \
  --json assets --jq '.assets[] | "\(.name)\t\(.updatedAt)"'

# Every URL must read .../releases/download/wsl-exe/...
grep "releases/download" bash/install.ps1
```

No edit to `install.ps1` is needed, and that is deliberate: a floating tag means the
installer in someone's Downloads folder from six months ago fetches the current assets.
The cost is that there is no way to install an older set, and no changelog in the tag —
so say what changed in the release notes.

> **`stable` ships separately.** `install.ps1` runs
> `curl .../celaut-project/nodo/stable/install.sh | sudo bash` inside the distro, so the
> node's own code comes from the `stable` branch at install time, not from `debian.tar`.
> Rebuilding the rootfs without moving `stable` ships nothing new; moving `stable`
> without rebuilding the rootfs still ships the change. **Moving `stable` is what
> releases a node to Windows users.**

### 5. Rebuild `Nodo-Setup.exe` (optional)

`Nodo-Setup.exe` is `install.ps1` compiled with PS2EXE, and it is uploaded to the same
`wsl-exe` release. It no longer embeds a tag that can go stale, but it does embed the
*script*, so it lags any change to `install.ps1` until rebuilt — which needs a Windows
build environment. Until then, point users at running `install.ps1` directly
([`WSL.md`](WSL.md#running-it)).

## Verify the release

After users run the new installer, the error sequence from issue #138
(`virtio_blk` / `virtio_blk.ko` not found) should be gone. To confirm locally:

```bash
# Inside the installed WSL distro, after install.sh completes:
sudo nodo doctor
```

All checks should pass, including the Cloud Hypervisor KVM smoke test.
