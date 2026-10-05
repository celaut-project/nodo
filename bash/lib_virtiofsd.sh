# virtiofsd, the daemon that serves a shared filesystem, for the setup scripts.
#
# Sourced by bash/setup_linux_arm.sh and bash/setup_linux_x86.sh after lib_pkg.sh
# and lib_rust.sh. The node starts one virtiofsd per share with the flags of the
# Rust implementation (src/virtualizers/microvm/virtiofs.py). No distro ships it on
# Ubuntu 22.04, and the C one in qemu-system-common does not take those flags, so
# before this a node had no usable daemon until an operator built one (#478).
#
# The order:
#
#   1. Keep an operator's own choice. When virtualizers.ch.VIRTIOFSD_BINARY is
#      anything other than the default, this file installs nothing and writes
#      nothing.
#   2. Use the static x86_64 build upstream publishes for each release. Its zip
#      and the binary in it are pinned below by SHA-256.
#   3. Otherwise (arm64, or the download failed) build the pinned version with
#      cargo, in the node's own Rust toolchain (lib_rust.sh).
#
# The binary goes to $TARGET_DIR/bin/virtiofsd, beside cloud-hypervisor, and the
# config gets that absolute path: the node runs as root and asks no user's PATH.
# Nothing here fails the install. Only services that declare shared directories
# need the daemon, and `nodo doctor` reports when it is missing.

VIRTIOFSD_VERSION="1.14.0"
# The upload attached to https://gitlab.com/virtio-fs/virtiofsd/-/releases/v1.14.0.
# Upstream builds it in CI for x86_64-unknown-linux-musl only. Bump the version,
# the URL and both digests together.
VIRTIOFSD_AMD64_ZIP_URL="https://gitlab.com/-/project/21523468/uploads/f505704014ae7a816e515f2a05a93d8b/virtiofsd-v1.14.0.zip"
VIRTIOFSD_AMD64_ZIP_SHA256="2e4fe9571f492b00baa34bc4e708e950039c5da05b830b31a8d179cb6ac8978e"
VIRTIOFSD_AMD64_ZIP_MEMBER="target/x86_64-unknown-linux-musl/release/virtiofsd"
VIRTIOFSD_AMD64_BIN_SHA256="15b2e72a78cc08a9bd8a6943e89fb69c88cb3cbeb63069efceade835342ac7d4"

virtiofsd_target() {
    printf '%s/bin/virtiofsd' "$TARGET_DIR"
}

virtiofsd_is_pinned_version() {
    # The Rust daemon prints "virtiofsd 1.14.0". The C one prints
    # "virtiofsd version 6.2.0 (...)", so it never matches.
    local binary="$1"
    [ -x "$binary" ] || return 1
    [ "$("$binary" --version 2>/dev/null | head -n 1)" = "virtiofsd ${VIRTIOFSD_VERSION}" ]
}

fetch_prebuilt_virtiofsd() {
    local destination="$1"
    local tmp_dir actual

    [ "$CH_ARCH_TAG" = "linux/amd64" ] || return 1

    tmp_dir="$(mktemp -d /tmp/nodo-virtiofsd.XXXXXX)"
    if ! download_file "$VIRTIOFSD_AMD64_ZIP_URL" "$tmp_dir/virtiofsd.zip"; then
        echo "Could not download virtiofsd ${VIRTIOFSD_VERSION} from ${VIRTIOFSD_AMD64_ZIP_URL}." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    actual="$(sha256sum "$tmp_dir/virtiofsd.zip" | awk '{print $1}')"
    if [ "$actual" != "$VIRTIOFSD_AMD64_ZIP_SHA256" ]; then
        echo "SHA256 mismatch for the virtiofsd ${VIRTIOFSD_VERSION} zip: expected ${VIRTIOFSD_AMD64_ZIP_SHA256}, got ${actual}." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    # The portable Python is installed by now, and its zipfile module means the
    # host needs no unzip.
    if ! "${PYTHON_RUNTIME_ROOT}/current/bin/python3" - "$tmp_dir/virtiofsd.zip" \
        "$VIRTIOFSD_AMD64_ZIP_MEMBER" "$tmp_dir/virtiofsd" <<'PY'
import shutil, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive, archive.open(sys.argv[2]) as src, \
        open(sys.argv[3], "wb") as dst:
    shutil.copyfileobj(src, dst)
PY
    then
        echo "Could not extract ${VIRTIOFSD_AMD64_ZIP_MEMBER} from the virtiofsd zip." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    actual="$(sha256sum "$tmp_dir/virtiofsd" | awk '{print $1}')"
    if [ "$actual" != "$VIRTIOFSD_AMD64_BIN_SHA256" ]; then
        echo "SHA256 mismatch for the virtiofsd ${VIRTIOFSD_VERSION} binary: expected ${VIRTIOFSD_AMD64_BIN_SHA256}, got ${actual}." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    mkdir -p "$(dirname "$destination")"
    install -m 0755 "$tmp_dir/virtiofsd" "$destination"
    rm -rf "$tmp_dir"
}

build_virtiofsd_from_source() {
    local destination="$1"
    local tmp_root

    install_virtiofsd_build_deps || return 1
    install_self_contained_rust || return 1

    echo "Building virtiofsd ${VIRTIOFSD_VERSION} with cargo (a few minutes)..."
    tmp_root="$(mktemp -d /tmp/nodo-virtiofsd-build.XXXXXX)"
    # --locked builds with the Cargo.lock upstream released, and crates.io checks
    # the digest of every crate it downloads.
    if ! RUSTUP_HOME="$RUST_RUNTIME_ROOT/rustup" CARGO_HOME="$RUST_RUNTIME_ROOT/cargo" \
        "$(rust_cargo_bin)" install virtiofsd --version "=${VIRTIOFSD_VERSION}" \
        --locked --root "$tmp_root"; then
        rm -rf "$tmp_root"
        return 1
    fi

    mkdir -p "$(dirname "$destination")"
    install -m 0755 "$tmp_root/bin/virtiofsd" "$destination"
    rm -rf "$tmp_root"
}

provision_virtiofsd() {
    local configured target

    target="$(virtiofsd_target)"
    configured="$(read_config_path_or_default '.virtualizers.ch.VIRTIOFSD_BINARY' 'virtiofsd')"
    case "$configured" in
        virtiofsd|"$target") ;;
        *)
            echo "virtualizers.ch.VIRTIOFSD_BINARY is ${configured}; keeping it and installing no virtiofsd."
            return 0
            ;;
    esac

    if virtiofsd_is_pinned_version "$target"; then
        echo "virtiofsd ${VIRTIOFSD_VERSION} is already installed at ${target}."
    elif fetch_prebuilt_virtiofsd "$target"; then
        echo "Installed the prebuilt virtiofsd ${VIRTIOFSD_VERSION} at ${target}."
    elif build_virtiofsd_from_source "$target"; then
        echo "Built virtiofsd ${VIRTIOFSD_VERSION} at ${target}."
    else
        echo "Warning: could not install virtiofsd ${VIRTIOFSD_VERSION}." >&2
        echo "  Services that declare shared directories cannot run on this node until it is installed." >&2
        echo "  See docs/SHARED_FILESYSTEMS.md; 'nodo doctor' reports its state." >&2
        return 0
    fi

    if ! virtiofsd_is_pinned_version "$target"; then
        echo "Warning: ${target} does not report virtiofsd ${VIRTIOFSD_VERSION}; config.yaml not changed." >&2
        return 0
    fi

    VIRTIOFSD_TARGET="$target" "$YQ_BIN" -i \
        '.virtualizers.ch.VIRTIOFSD_BINARY = strenv(VIRTIOFSD_TARGET)' \
        "$CONFIG_FILE"
}
