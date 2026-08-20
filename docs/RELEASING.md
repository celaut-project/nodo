# Releasing Nodo WSL (Windows)

## Overview

The Windows installer (`bash/install.ps1` / `Nodo-Setup.exe`) downloads assets from a
**hardcoded GitHub release tag**. When a new release is cut, the tag references inside
`bash/install.ps1` must be bumped to match.

## Release assets

| File | Purpose | Notes |
|---|---|---|
| `debian.tar` | WSL2 rootfs imported by the installer | Ubuntu 22.04 base with nodo code + venv pre-installed |
| `vmlinuz` | Cloud Hypervisor **guest** kernel | Downloaded by `setup_linux_x86.sh` from the `guest-kernel-vN` release |
| `bzImage` | WSL2 **host** kernel | Written to `C:\wsl-kernel\bzImage` and referenced in `.wslconfig`; currently Josemi's custom `microhobby` build — requires a separate kernel build environment to update |
| `initramfs` | Pre-built staging initramfs | Placed at `/boot/initramfs` inside the distro before `install.sh` runs; overwritten during install by `build_ch_initramfs.sh` — its content is not load-bearing, but it must exist |

## When to cut a release

- Any change to `bash/build_ch_initramfs.sh` (initramfs modules, virtio support)
- Any change to `bash/setup_linux_x86.sh` (setup flow, dependency versions)
- Significant feature merges that should ship to Windows users

After creating the release, bump the hardcoded version tag in `bash/install.ps1`
and open a PR (see "Bump version references" below).

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

# Start a privileged container (--privileged required for modprobe inside build_ch_initramfs.sh)
docker run --privileged -d --name nodo-v2-build ubuntu:22.04 sleep infinity

# Copy nodo code into the container filesystem (NOT a bind mount — must be inside for docker export)
docker cp /tmp/nodo-release-build/. nodo-v2-build:/nodo/
```

### 2. Run setup

`setup_linux_x86.sh` downloads the guest kernel from the `guest-kernel-vN` release, so the build
container needs no kernel package of its own (`/boot` is never read, and the initramfs no longer
packs host modules).

```bash
# Copy the example config so setup_linux_x86.sh can find config.yaml
docker exec nodo-v2-build cp /nodo/config.example.yaml /nodo/config.yaml

# Run the full setup — installs portable Python, Cloud Hypervisor binary,
# builds initramfs via build_ch_initramfs.sh, creates venv, runs migrations
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

### 4. Create the GitHub release

```bash
NEXT_TAG=v2   # increment as needed (v3, v4, …)

gh release create "$NEXT_TAG" \
  --repo celaut-project/nodo \
  --title "Nodo WSL ${NEXT_TAG} [Windows 11 - x86_64]" \
  --notes "Rebuilt from dev HEAD. Fixes: initramfs includes virtio_blk/virtio_net/virtiofs modules." \
  --latest \
  /tmp/debian.tar#debian.tar \
  /tmp/vmlinuz#vmlinuz \
  /tmp/bzImage#bzImage \
  /tmp/initramfs#initramfs
```

### 5. Bump version references in install.ps1

`bash/install.ps1` has four hardcoded references to the previous tag. Update all of them:

```bash
# In the nodo repo, on a new branch:
sed -i "s|/releases/download/v[0-9]*/|/releases/download/${NEXT_TAG}/|g" bash/install.ps1
```

Verify:
```bash
grep "releases/download" bash/install.ps1
```

Commit and open a PR targeting `dev`.

> `Nodo-Setup.exe` is a compiled GUI wrapper around `install.ps1`. It also embeds the old
> tag and must be rebuilt separately (requires a Windows build environment with PS2EXE or
> the equivalent). Until it is rebuilt, users should run `install.ps1` directly.

## Verify the release

After users run the new installer, the error sequence from issue #138
(`virtio_blk` / `virtio_blk.ko` not found) should be gone. To confirm locally:

```bash
# Inside the installed WSL distro, after install.sh completes:
sudo nodo doctor
```

All checks should pass, including the Cloud Hypervisor KVM smoke test.
