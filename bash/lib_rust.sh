# Rust and the `nodo tui` binary, for the setup scripts.
#
# Sourced by bash/setup_linux_arm.sh and bash/setup_linux_x86.sh, the way
# lib_pkg.sh already is: the two scripts differ only in the architecture they
# pin, and a second copy of this logic would drift in exactly the way the
# original bug did.
#
# Two things happen here, in this order, and the order is the point:
#
#   1. Try to fetch the `tui` binary CI built for this host, with the `rustc -vV`
#      host triple recorded beside it. A node that never compiles anything needs
#      no compiler, and most nodes are that node.
#   2. Only if that fails (offline, no release yet, an unsupported host), install
#      Rust -- into $TARGET_DIR/runtime/rust, with RUSTUP_HOME and CARGO_HOME
#      pointed at it and --no-modify-path so no shell rc file is touched.
#
# (2) is what issue #375 is about. The old block was:
#
#     if ! command -v cargo >/dev/null; then
#         curl ... | sh -s -- -y
#         source "$HOME/.cargo/env"
#     fi
#
# which installs into $HOME/.cargo and exports a PATH that dies with this
# script's subshell. Nothing that runs later -- `nodo tui`, the systemd unit, a
# desktop shortcut -- inherits it, so a node with a real toolchain reported it
# missing and reinstalled it on every launch. The node now owns a toolchain at a
# path it can compute, and asks nobody's $PATH about it.
#
# An existing ~/.cargo is deliberately left alone: not migrated, not deleted, not
# consulted. It is the operator's, and a setup script that moves it would be
# taking something that is not the node's to take.

# Where CI publishes. One mutable tag, in the shape guest-kernel.yml established:
# the binary is rebuilt whenever the crate changes, and there is nothing to
# version independently of the repository it lives in.
RUST_TUI_RELEASE_TAG="${RUST_TUI_RELEASE_TAG:-tui}"
RUST_TUI_RELEASE_REPO="${RUST_TUI_RELEASE_REPO:-celaut-project/nodo}"

rust_runtime_root() {
    printf '%s' "$RUST_RUNTIME_ROOT"
}

rust_cargo_bin() {
    printf '%s/cargo/bin/cargo' "$RUST_RUNTIME_ROOT"
}

rust_toolchain_ready() {
    # Present AND runnable. A half-finished rustup leaves the directory behind,
    # and trusting the directory turns "no toolchain" into a linker error deep
    # inside a build.
    local cargo
    cargo="$(rust_cargo_bin)"
    [ -x "$cargo" ] || return 1
    RUSTUP_HOME="$RUST_RUNTIME_ROOT/rustup" CARGO_HOME="$RUST_RUNTIME_ROOT/cargo" \
        "$cargo" --version >/dev/null 2>&1
}

install_self_contained_rust() {
    local cargo
    cargo="$(rust_cargo_bin)"

    if rust_toolchain_ready; then
        echo "Rust already installed at ${RUST_RUNTIME_ROOT}."
        return 0
    fi

    echo "Installing Rust into ${RUST_RUNTIME_ROOT} (this node's own, not the user's)..."
    mkdir -p "$RUST_RUNTIME_ROOT/rustup" "$RUST_RUNTIME_ROOT/cargo"

    # --no-modify-path is the whole point: the toolchain announces itself by
    # living at a path nodo computes, never by editing a profile nobody re-reads.
    if ! RUSTUP_HOME="$RUST_RUNTIME_ROOT/rustup" CARGO_HOME="$RUST_RUNTIME_ROOT/cargo" \
        sh -c "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path"; then
        return 1
    fi

    rust_toolchain_ready || return 1
    echo "Rust installed at ${RUST_RUNTIME_ROOT}."
}

fetch_prebuilt_tui() {
    # The `tui` binary CI built for this architecture, plus the host triple it was
    # built for. Both land where cargo would have put them, so a prebuilt and a
    # locally built binary are the same file to `nodo tui` -- there is no second
    # location to keep in sync, and rebuilding from source simply overwrites it.
    #
    # The published SHA256SUMS proves the download was not truncated or
    # intercepted in transit. It does NOT pin content the way
    # guest-kernel/SHA256SUMS.pinned does: that file can pin because the guest is
    # built from sources this repository does not move, whereas this binary is
    # built FROM this repository, so an in-tree digest of it would have to be
    # updated by the same commit that changes the crate -- which cannot be done
    # before CI has built it. Trust here is the release, and the fallback below
    # is what makes that acceptable: a node that does not get a binary compiles
    # one from the source it already has.
    local asset="tui-${RUST_TUI_ASSET_TAG}"
    local base_url="https://github.com/${RUST_TUI_RELEASE_REPO}/releases/download/${RUST_TUI_RELEASE_TAG}"
    local dest_dir="$TARGET_DIR/src/commands/tui/target/release"
    local tmp_bin tmp_marker tmp_sums expected actual recorded

    tmp_bin="$(mktemp /tmp/nodo-tui.XXXXXX)"
    tmp_marker="$(mktemp /tmp/nodo-tui-marker.XXXXXX)"
    tmp_sums="$(mktemp /tmp/nodo-tui-sums.XXXXXX)"

    if ! download_file "${base_url}/${asset}" "$tmp_bin" \
        || ! download_file "${base_url}/${asset}.host-triple" "$tmp_marker" \
        || ! download_file "${base_url}/SHA256SUMS" "$tmp_sums"; then
        rm -f "$tmp_bin" "$tmp_marker" "$tmp_sums"
        return 1
    fi

    expected="$(awk -v name="$asset" '$2 == name || $2 == "*"name { print $1; exit }' "$tmp_sums")"
    actual="$(sha256sum "$tmp_bin" | awk '{print $1}')"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "Prebuilt tui checksum did not match (expected='${expected}' actual='${actual}')." >&2
        rm -f "$tmp_bin" "$tmp_marker" "$tmp_sums"
        return 1
    fi

    # The marker is what `nodo tui` checks before executing the binary, so a
    # marker that does not name the target we asked for means the release is
    # mislabelled -- and running it anyway is the "Exec format error the operator
    # sees as nothing happening" case this exists to prevent.
    recorded="$(head -n 1 "$tmp_marker" | tr -d '[:space:]')"
    if [ -z "$recorded" ] || [ "$recorded" != "$RUST_TUI_HOST_TRIPLE" ]; then
        echo "Prebuilt tui is for '${recorded:-unknown}', this host is '${RUST_TUI_HOST_TRIPLE}'." >&2
        rm -f "$tmp_bin" "$tmp_marker" "$tmp_sums"
        return 1
    fi

    mkdir -p "$dest_dir"
    install -m 0755 "$tmp_bin" "$dest_dir/tui"
    install -m 0644 "$tmp_marker" "$dest_dir/tui.host-triple"
    rm -f "$tmp_bin" "$tmp_marker" "$tmp_sums"

    "$dest_dir/tui" --version >/dev/null 2>&1 || {
        echo "Prebuilt tui does not run on this host; removing it." >&2
        rm -f "$dest_dir/tui" "$dest_dir/tui.host-triple"
        return 1
    }

    echo "Installed prebuilt tui (${recorded}) at ${dest_dir}/tui."
}

provision_rust_and_tui() {
    # What this function decides: whether this install ends up with a compiler.
    #
    # `dependencies.rust.INSTALL_TOOLCHAIN: true` (or NODO_INSTALL_RUST=1) forces
    # one in regardless, which is what a developer wants -- they are going to
    # rebuild the crate, and discovering at that moment that the node was shipped
    # a binary instead of a toolchain is a worse time to find out.
    local force_toolchain
    force_toolchain="$(read_config_path_or_default '.dependencies.rust.INSTALL_TOOLCHAIN' "${NODO_INSTALL_RUST:-false}")"

    case "$force_toolchain" in
        true|True|TRUE|1|yes)
            echo "dependencies.rust.INSTALL_TOOLCHAIN is set; installing the toolchain."
            install_self_contained_rust || fail "Failed to install Rust into ${RUST_RUNTIME_ROOT}."
            return 0
            ;;
    esac

    if fetch_prebuilt_tui; then
        echo "Skipping the Rust toolchain: this node has a tui binary and compiles nothing."
        echo "  Set dependencies.rust.INSTALL_TOOLCHAIN: true to install it anyway."
        return 0
    fi

    echo "No usable prebuilt tui for this host; installing Rust to build it from source."
    if ! install_self_contained_rust; then
        # Not fatal. Every other nodo command works without a TUI, and an install
        # that aborts here leaves a node that would otherwise serve.
        echo "Warning: could not install Rust into ${RUST_RUNTIME_ROOT}." >&2
        echo "  Everything except 'nodo tui' works; it will retry the install on first run." >&2
    fi
}
