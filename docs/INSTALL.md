# Manual Installation Guide

This guide is for Linux users who want to bootstrap Nodo manually, without executing `install.sh`.
Java is optional and only required for Ergo-backed payment/reputation features.

It follows the current runtime model:

- Local runtimes and binaries under `MAIN_DIR` (Python, Java, yq).
- Cloud Hypervisor assets under `MAIN_DIR/cloud_hypervisor` (services run as microVMs).
- No local Docker by default: packing is delegated to an external packer-service.
  (Docker is only provisioned locally, in isolation, if you opt into
  `packer.local: true`; see step 9.)

## 1) Scope and assumptions

- OS: any Linux with `apt` or `dnf` (tested on Ubuntu 22.04/24.04 and Fedora 44,
  x86_64 and aarch64 — including Fedora Asahi Remix on Apple Silicon).
- Architecture: `x86_64` or `aarch64`.
- You have `sudo` access.
- Installation root: `TARGET_DIR` (default `/nodo`).

```bash
export TARGET_DIR=/nodo
```

## 2) Install base system packages

Host-level tools only. Nodo installs its own Python, JRE, yq, cloud-hypervisor and
guest kernel in later steps, so nothing here is a build dependency of CPython.
These are the same packages `bash/lib_pkg.sh` installs for you when you run
`install.sh`.

**Debian / Ubuntu:**

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential clang cpio gzip zip \
  curl ca-certificates git procps iproute2 iputils-ping \
  iptables e2fsprogs locales
```

**Fedora / RHEL:**

```bash
sudo dnf install -y \
  gcc make clang cpio gzip zip \
  curl ca-certificates git procps-ng iproute iputils \
  iptables-nft e2fsprogs glibc-langpack-en
```

Why these:

- `clang` — the portable CPython records `CC=clang` in its `sysconfig`, and
  `psutil` has no `linux-aarch64` wheel, so pip compiles it from source. Without a
  compiler the install fails at `pip install -r requirements.txt`. (`gcc` works
  too if you `export CC=gcc CXX=g++`.)
- `cpio`, `gzip` — pack the Cloud Hypervisor initramfs. Its only binary, a static
  busybox, is downloaded from the Nodo release in step 10, so no busybox package
  is needed here.
- `iproute2`/`iproute` and `zip` are load-bearing at runtime: `ip` is a hard
  preflight requirement for `execute` (CH networking), and `zip` is invoked when
  packing — without them the first `execute`/`pack` fails. `iptables` and
  `e2fsprogs` provide the `iptables`/`debugfs` tools also checked by that preflight.
- `protobuf-compiler` is not installed by the setup script and is only needed for
  development (regenerating protobufs), so it is omitted here.

If your distro uses neither `apt` nor `dnf`, install the equivalents by hand and
add a branch to `pkg_for()` in `bash/lib_pkg.sh` so `install.sh` works there too.

## 3) Get source and create config

```bash
git clone https://github.com/celaut-project/nodo.git "$TARGET_DIR"
cd "$TARGET_DIR"
cp -n config.example.yaml config.yaml
```

## 4) Bootstrap local yq and set base config

Install a bootstrap `yq` binary first (used to edit/read `config.yaml`).

```bash
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) YQ_ASSET=yq_linux_amd64 ;;
  aarch64|arm64) YQ_ASSET=yq_linux_arm64 ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

mkdir -p "$TARGET_DIR/bin"
curl -fsSL "https://github.com/mikefarah/yq/releases/download/v4.44.3/${YQ_ASSET}" -o "$TARGET_DIR/bin/yq"
chmod +x "$TARGET_DIR/bin/yq"
```

Align `main.*` paths with your installation root:

```bash
"$TARGET_DIR/bin/yq" -i \
  '.main.MAIN_DIR = env(TARGET_DIR) |
   .main.STORAGE = env(TARGET_DIR) + "/storage" |
   .main.CACHE = .main.STORAGE + "/__cache__/" |
   .main.REGISTRY = .main.STORAGE + "/__registry__/" |
   .main.METADATA_REGISTRY = .main.STORAGE + "/__metadata__/" |
   .main.BLOCKDIR = .main.STORAGE + "/__block__/" |
   .main.DATABASE_FILE = .main.STORAGE + "/database.sqlite"' \
  "$TARGET_DIR/config.yaml"
```

Optional: override runtime/binary locations in `dependencies.*` before continuing. Example (custom roots):

```bash
"$TARGET_DIR/bin/yq" -i \
  '.dependencies.python.RUNTIME_ROOT = "${main.MAIN_DIR}/runtime/python" |
   .dependencies.python.RUNTIME_BIN = "${main.MAIN_DIR}/runtime/python/current/bin/python3" |
   .dependencies.python.VENV_BIN = "${main.MAIN_DIR}/venv/bin/python" |
   .dependencies.java.RUNTIME_ROOT = "${main.MAIN_DIR}/runtime/java" |
   .dependencies.java.JAVA_HOME = "${main.MAIN_DIR}/runtime/java/current" |
   .dependencies.yq.BIN = "${main.MAIN_DIR}/bin/yq"' \
  "$TARGET_DIR/config.yaml"
```

## 5) Resolve effective paths from `config.yaml`

Run this block once and keep the shell session open. Later commands rely on these variables.

```bash
expand_main_dir() {
  printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_cfg_path_or_default() {
  local query="$1"
  local fallback="$2"
  local raw
  raw="$("$TARGET_DIR/bin/yq" -r "$query // \"\"" "$TARGET_DIR/config.yaml")"
  if [ -z "$raw" ] || [ "$raw" = "null" ]; then
    raw="$fallback"
  fi
  expand_main_dir "$raw"
}

YQ_BIN="$(read_cfg_path_or_default '.dependencies.yq.BIN' '${main.MAIN_DIR}/bin/yq')"
mkdir -p "$(dirname "$YQ_BIN")"
if [ "$YQ_BIN" != "$TARGET_DIR/bin/yq" ]; then
  cp "$TARGET_DIR/bin/yq" "$YQ_BIN"
  chmod +x "$YQ_BIN"
fi

PY_RUNTIME_ROOT="$(read_cfg_path_or_default '.dependencies.python.RUNTIME_ROOT' '${main.MAIN_DIR}/runtime/python')"
PY_RUNTIME_BIN="$(read_cfg_path_or_default '.dependencies.python.RUNTIME_BIN' '${main.MAIN_DIR}/runtime/python/current/bin/python3')"
PY_VENV_BIN="$(read_cfg_path_or_default '.dependencies.python.VENV_BIN' '${main.MAIN_DIR}/venv/bin/python')"
PY_VENV_DIR="$(dirname "$(dirname "$PY_VENV_BIN")")"

JAVA_RUNTIME_ROOT="$(read_cfg_path_or_default '.dependencies.java.RUNTIME_ROOT' '${main.MAIN_DIR}/runtime/java')"
JAVA_HOME_PATH="$(read_cfg_path_or_default '.dependencies.java.JAVA_HOME' '${main.MAIN_DIR}/runtime/java/current')"

CH_BINARY_PATH="$(read_cfg_path_or_default '.virtualizers.ch.BINARY_PATH' '${main.MAIN_DIR}/bin/cloud-hypervisor')"
```

## 6) Install local portable Python runtime

Pinned versions used by project setup:

- Python: `3.11.15`
- python-build-standalone tag: `20260325`

```bash
PY_VER="3.11.15"
PY_TAG="20260325"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) PY_ARCH="x86_64-unknown-linux-gnu" ;;
  aarch64|arm64) PY_ARCH="aarch64-unknown-linux-gnu" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

PY_DIST="cpython-${PY_VER}+${PY_TAG}-${PY_ARCH}-install_only_stripped.tar.gz"
PY_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}"

mkdir -p "$PY_RUNTIME_ROOT"
curl -fsSL "$PY_BASE/$PY_DIST" -o /tmp/nodo-python.tar.gz
curl -fsSL "$PY_BASE/SHA256SUMS" -o /tmp/nodo-python.SHA256SUMS

EXPECTED="$(awk -v name="$PY_DIST" '{f=$2; gsub(/^\*/,"",f); if (f==name || $NF==name) {print $1; exit}}' /tmp/nodo-python.SHA256SUMS)"
ACTUAL="$(sha256sum /tmp/nodo-python.tar.gz | awk '{print $1}')"
[ "$EXPECTED" = "$ACTUAL" ] || { echo "Python checksum mismatch"; exit 1; }

PY_INSTALL_DIR="$PY_RUNTIME_ROOT/${PY_VER}+${PY_TAG}"
mkdir -p "$PY_INSTALL_DIR"
TMP_PY_DIR="$(mktemp -d)"
tar -xzf /tmp/nodo-python.tar.gz -C "$TMP_PY_DIR"
cp -a "$TMP_PY_DIR"/*/. "$PY_INSTALL_DIR" 2>/dev/null || cp -a "$TMP_PY_DIR"/. "$PY_INSTALL_DIR"
ln -sfn "$PY_INSTALL_DIR" "$PY_RUNTIME_ROOT/current"

PY_CURRENT_BIN="$PY_RUNTIME_ROOT/current/bin/python3"
if [ "$PY_RUNTIME_BIN" != "$PY_CURRENT_BIN" ]; then
  mkdir -p "$(dirname "$PY_RUNTIME_BIN")"
  ln -sfn "$PY_CURRENT_BIN" "$PY_RUNTIME_BIN"
fi

rm -rf "$TMP_PY_DIR" /tmp/nodo-python.tar.gz /tmp/nodo-python.SHA256SUMS
```

## 7) Install local portable Java runtime (Temurin JRE 21)

Pinned version used by project setup:

- JRE: `21.0.8_9` (`jdk-21.0.8+9` release tag)

```bash
JRE_VER="21.0.8_9"
JRE_TAG="jdk-21.0.8%2B9"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) JRE_DIST="OpenJDK21U-jre_x64_linux_hotspot_${JRE_VER}.tar.gz" ;;
  aarch64|arm64) JRE_DIST="OpenJDK21U-jre_aarch64_linux_hotspot_${JRE_VER}.tar.gz" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

JRE_URL="https://github.com/adoptium/temurin21-binaries/releases/download/${JRE_TAG}/${JRE_DIST}"

mkdir -p "$JAVA_RUNTIME_ROOT"
curl -fsSL "$JRE_URL" -o /tmp/nodo-jre.tar.gz
curl -fsSL "${JRE_URL}.sha256.txt" -o /tmp/nodo-jre.sha256.txt

EXPECTED="$(awk '{print $1}' /tmp/nodo-jre.sha256.txt | head -n1)"
ACTUAL="$(sha256sum /tmp/nodo-jre.tar.gz | awk '{print $1}')"
[ "$EXPECTED" = "$ACTUAL" ] || { echo "JRE checksum mismatch"; exit 1; }

JRE_INSTALL_DIR="$JAVA_RUNTIME_ROOT/${JRE_VER}"
mkdir -p "$JRE_INSTALL_DIR"
TMP_JRE_DIR="$(mktemp -d)"
tar -xzf /tmp/nodo-jre.tar.gz -C "$TMP_JRE_DIR"
cp -a "$TMP_JRE_DIR"/*/. "$JRE_INSTALL_DIR" 2>/dev/null || cp -a "$TMP_JRE_DIR"/. "$JRE_INSTALL_DIR"
ln -sfn "$JRE_INSTALL_DIR" "$JAVA_RUNTIME_ROOT/current"

JAVA_CURRENT_HOME="$JAVA_RUNTIME_ROOT/current"
if [ "$JAVA_HOME_PATH" != "$JAVA_CURRENT_HOME" ] && [ "$JAVA_HOME_PATH" != "$JRE_INSTALL_DIR" ]; then
  mkdir -p "$(dirname "$JAVA_HOME_PATH")"
  ln -sfn "$JRE_INSTALL_DIR" "$JAVA_HOME_PATH"
fi

rm -rf "$TMP_JRE_DIR" /tmp/nodo-jre.tar.gz /tmp/nodo-jre.sha256.txt
```

## 8) Create local Python venv and install Python deps

```bash
mkdir -p "$PY_VENV_DIR"
"$PY_RUNTIME_BIN" -m venv "$PY_VENV_DIR"
"$PY_VENV_BIN" -m pip install --upgrade pip
"$PY_VENV_BIN" -m pip install -r "$TARGET_DIR/bash/requirements.txt"
```

## 9) Docker is not installed by default

Docker is **not** installed at install time, and service **execution** never uses
it — services run as **Cloud Hypervisor** microVMs. Docker is only relevant to the
*packer*, and only in the opt-in local mode:

- **Default (`packer.local: false`) — no Docker on this host.** `nodo pack`
  delegates the build to a **packer-service** (it runs Docker/buildx inside its
  own sealed microVM). Set the packer-service's published service id under
  `core_services` in `config.yaml` — the single source of truth:
  `core_services: { packer: "<id>" }` — and `nodo execute` it so a
  running instance exists; nodo resolves that instance's `ip:port` automatically.
  When nodo needs to download the packer, it fetches it directly from
  `packer.PACKER_SOURCE_URL` if set, otherwise via the source-application core
  service. To point at an out-of-band packer instead, set
  `packer.PACKER_SERVICE_URL` to its `ip:8080` as an override.

- **Opt-in (`packer.local: true`) — rootless local builder.** If you set
  `packer.local: true`, `nodo pack` builds on this host instead. Docker is still
  never installed; the first local pack provisions a **rootless BuildKit**
  toolchain on demand via `bash/install_buildkit.sh` (mirroring `install_java.sh`)
  and drives its own builder under `MAIN_DIR`. The builder runs as the invoking
  user, so packing needs no privileges; provisioning the host prerequisites for
  rootless builds (`uidmap`, `rootlesskit`, subordinate id ranges) may ask for
  sudo once, on that first install. See `dependencies.buildkit.*` and
  `packer.buildkit.*` in `config.yaml`. Full packing reference:
  [`PACKING.md`](PACKING.md).

## 10) Install Cloud Hypervisor assets

Pinned version used by project setup:

- Cloud Hypervisor: `v51.1`

```bash
CH_VERSION="v51.1"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)
    CH_ARCH_TAG="linux/amd64"
    CH_ASSET_CANDIDATES=("cloud-hypervisor-static" "cloud-hypervisor-static-x86_64")
    ;;
  aarch64|arm64)
    CH_ARCH_TAG="linux/arm64"
    CH_ASSET_CANDIDATES=("cloud-hypervisor-static-aarch64" "cloud-hypervisor-static-arm64")
    ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

mkdir -p "$(dirname "$CH_BINARY_PATH")"
CH_OK=""
for A in "${CH_ASSET_CANDIDATES[@]}"; do
  if curl -fsSL "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/${CH_VERSION}/${A}" -o /tmp/cloud-hypervisor.bin; then
    install -m 0755 /tmp/cloud-hypervisor.bin "$CH_BINARY_PATH"
    CH_OK=1
    break
  fi
done
[ -n "$CH_OK" ] || { echo "Unable to download cloud-hypervisor"; exit 1; }
rm -f /tmp/cloud-hypervisor.bin

# Guest kernel: a Nodo release asset, never the host's /boot kernel. It is built
# by .github/workflows/guest-kernel.yml from bash/guest-kernel/ and pinned in
# install.sh as GUEST_KERNEL_VERSION.
GUEST_KERNEL_VERSION="guest-kernel"
GUEST_KERNEL_ASSET="vmlinuz-${CH_ARCH_TAG/\//-}"   # linux/arm64 -> vmlinuz-linux-arm64
GUEST_KERNEL_BASE="https://github.com/celaut-project/nodo/releases/download/${GUEST_KERNEL_VERSION}"

CH_KERNEL_TARGET="$TARGET_DIR/cloud_hypervisor/kernels/${CH_ARCH_TAG}/vmlinuz"
CH_INITRAMFS_TARGET="$TARGET_DIR/cloud_hypervisor/initramfs/${CH_ARCH_TAG}/initramfs"
mkdir -p "$(dirname "$CH_KERNEL_TARGET")" "$(dirname "$CH_INITRAMFS_TARGET")"

CH_BUSYBOX_TARGET="$TARGET_DIR/cloud_hypervisor/busybox/${CH_ARCH_TAG}/busybox"
mkdir -p "$(dirname "$CH_BUSYBOX_TARGET")"

# Expected digests come from bash/guest-kernel/SHA256SUMS.pinned in this checkout,
# NOT from the SHA256SUMS published next to the artifact: that one lives in the same
# mutable release, so it would only prove the download was not truncated.
GUEST_SUMS="$TARGET_DIR/bash/guest-kernel/SHA256SUMS.pinned"
PINNED_TAG="$(awk '$1 == "TAG" { print $2; exit }' "$GUEST_SUMS")"
[ "$PINNED_TAG" = "$GUEST_KERNEL_VERSION" ] \
  || { echo "Pin is for $PINNED_TAG, not $GUEST_KERNEL_VERSION"; exit 1; }

fetch_guest_asset() {  # <asset-name> <destination> <mode>
  EXPECTED="$(awk -v n="$1" '$2 == n { print $1; exit }' "$GUEST_SUMS")"
  [ -n "$EXPECTED" ] || { echo "no pinned digest for $1"; exit 1; }
  curl -fsSL "${GUEST_KERNEL_BASE}/$1" -o /tmp/nodo-guest-asset
  ACTUAL="$(sha256sum /tmp/nodo-guest-asset | awk '{print $1}')"
  [ "$EXPECTED" = "$ACTUAL" ] || { echo "$1 does not match the pinned digest"; exit 1; }
  install -m "$3" /tmp/nodo-guest-asset "$2"
  rm -f /tmp/nodo-guest-asset
}

# The guest kernel, and the static busybox that is the guest's entire userspace.
fetch_guest_asset "$GUEST_KERNEL_ASSET" "$CH_KERNEL_TARGET" 0644
fetch_guest_asset "busybox-${CH_ARCH_TAG/\//-}" "$CH_BUSYBOX_TARGET" 0755

bash "$TARGET_DIR/bash/build_ch_initramfs.sh" "$TARGET_DIR" "$CH_ARCH_TAG" "$CH_INITRAMFS_TARGET"

CH_BINARY_PATH="$CH_BINARY_PATH" "$YQ_BIN" -i '.virtualizers.ch.BINARY_PATH = strenv(CH_BINARY_PATH)' "$TARGET_DIR/config.yaml"
CH_ARCH_TAG="$CH_ARCH_TAG" CH_KERNEL_TARGET="$CH_KERNEL_TARGET" "$YQ_BIN" -i \
  '.virtualizers.ch.KERNEL_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_KERNEL_TARGET)' \
  "$TARGET_DIR/config.yaml"
CH_ARCH_TAG="$CH_ARCH_TAG" CH_INITRAMFS_TARGET="$CH_INITRAMFS_TARGET" "$YQ_BIN" -i \
  '.virtualizers.ch.INITRAMFS_PATHS[strenv(CH_ARCH_TAG)] = strenv(CH_INITRAMFS_TARGET)' \
  "$TARGET_DIR/config.yaml"
```

## 11) Rootless local builder directories

Nodo never uses the host Docker daemon. When packing with `packer.local: true`,
the first local pack lazily starts a private, rootless BuildKit builder and
creates its state under `$TARGET_DIR/buildkit`:

- `$TARGET_DIR/buildkit/data` — builder state (`--root`)
- `$TARGET_DIR/buildkit/run` — the worker's `XDG_RUNTIME_DIR`
- `$TARGET_DIR/buildkit/buildkitd.sock` — the builder's private socket
- `$TARGET_DIR/buildkit/buildkitd.log` — daemon log, dumped on a failed start

These are created automatically on the first local pack (nothing to do here at
install time) and the builder is stopped again after each pack. Because it runs
as the invoking user, nodo can always stop it — no privileged signal involved.
`uninstall.sh` removes this tree during teardown.

## 12) Run DB migration

```bash
"$PY_VENV_BIN" "$TARGET_DIR/nodo.py" migrate
```

## 13) Configure service and wrapper

Create the `systemd` unit from template:

```bash
PY_RUNTIME_DIR="$(dirname "$PY_RUNTIME_BIN")"

# The admin group is distro-specific (`sudo` on Debian, `wheel` on Fedora/RHEL) and
# systemd refuses to start a unit whose Group does not resolve. install.sh and
# `nodo doctor` pick it the same way, so keep this in sync or doctor will rewrite
# the unit — and stop the service — on every run.
ADMIN_GROUP=root
for g in sudo wheel; do getent group "$g" >/dev/null 2>&1 && { ADMIN_GROUP="$g"; break; }; done

sudo sed \
  -e "s|{{MAIN_DIR}}|$TARGET_DIR|g" \
  -e "s|{{JAVA_HOME}}|$JAVA_HOME_PATH|g" \
  -e "s|{{PYTHON_RUNTIME_BIN_DIR}}|$PY_RUNTIME_DIR|g" \
  -e "s|{{PYTHON_VENV_BIN}}|$PY_VENV_BIN|g" \
  -e "s|{{ADMIN_GROUP}}|$ADMIN_GROUP|g" \
  "$TARGET_DIR/bash/nodo.service.template" > /tmp/nodo.service

sudo install -m 0644 /tmp/nodo.service /etc/systemd/system/nodo.service
sudo systemctl daemon-reload
sudo systemctl enable --now nodo.service
```

Optional wrapper command (`/usr/local/bin/nodo`) with same env as service:

```bash
sudo tee /usr/local/bin/nodo >/dev/null <<WRAP
#!/bin/bash
ORIGINAL_DIR="\$PWD"
cd "$TARGET_DIR" || exit 1
export JAVA_HOME="$JAVA_HOME_PATH"
export PATH="$JAVA_HOME_PATH/bin:$PY_RUNTIME_DIR:$TARGET_DIR/bin:\$PATH"
source "$PY_VENV_DIR/bin/activate"
ORIGINAL_DIR="\$ORIGINAL_DIR" "$PY_VENV_BIN" "$TARGET_DIR/nodo.py" "\$@"
WRAP
sudo chmod +x /usr/local/bin/nodo
```

## 14) Post-install checks

```bash
systemctl status nodo.service --no-pager
"$PY_VENV_BIN" "$TARGET_DIR/nodo.py" info
```

Cloud Hypervisor checks:

```bash
test -x "$CH_BINARY_PATH"
test -f "$CH_KERNEL_TARGET"
test -f "$CH_INITRAMFS_TARGET"
```

## 15) Operational notes

- Cross-arch builds are disabled in this profile. If target architecture differs from host architecture, build/pack flows fail early with an explicit message.
- QEMU/binfmt are intentionally not installed.
- Keep `config.yaml` and actual installed paths aligned. If you move runtimes/binaries, update `dependencies.*` and restart `nodo.service`.
