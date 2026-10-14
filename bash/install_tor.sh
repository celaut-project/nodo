#!/bin/bash
# install_tor.sh
# Installs nodo's OWN copy of Tor under MAIN_DIR/runtime/tor (dependencies.tor.RUNTIME_ROOT).
# Nothing is installed system-wide, /etc/tor is never read and no sudo is needed.
#
# It is optional and on demand, like install_buildkit.sh: nodo runs it when the operator
# asks (`nodo tor install`, or routing.tor.ENABLED: true on the next start).
#
# Layout, the same on both architectures (the node runs <root>/current/tor/tor):
#   <root>/<version>/tor/tor            the executable, with its libraries beside it
#   <root>/current -> <version>
#
#   x86_64   the Tor Project's Tor Expert Bundle: a prebuilt tor with its libraries.
#   aarch64  no prebuilt Linux/arm64 bundle exists, so tor is built from the official
#            source tarball. That needs a C compiler, make and the libevent, OpenSSL and
#            zlib development headers (the script says which packages if one is missing).
#
# Every download is checked against a digest pinned below. Bump the version and the
# digests together.
#
# Usage: bash install_tor.sh <MAIN_DIR>

set -euo pipefail

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Error: You must pass the project root directory as the first argument."
    exit 1
fi

TARGET_DIR="$(cd "$TARGET_DIR" >/dev/null 2>&1 && pwd)"
CONFIG_FILE="$TARGET_DIR/config.yaml"
YQ_BIN="$TARGET_DIR/bin/yq"

# tor 0.4.9.14 is what Tor Browser 15.0.25 ships; nodo needs 0.4.8 or later.
TOR_VERSION="0.4.9.14"
TOR_BROWSER_VERSION="15.0.25"
TOR_BUNDLE_SHA256_linux_x86_64="19cf18726eecb362e0f21d27afa9ea9efe5da928bfdfbb1ceb5fe54519e87f4a"
TOR_SOURCE_SHA256="182449ac1c8ff43278da27b69b02040e11d03d0327db6d9126e4190ec6235e4a"

fail() {
    echo "Error: $1" >&2
    exit 1
}

download_file() {
    local url="$1"
    local destination="$2"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$destination"
        return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -qO "$destination" "$url"
        return 0
    fi
    fail "Neither curl nor wget is available to download ${url}"
}

verify_sha256() {
    local file="$1"
    local expected="$2"
    local actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    [ "$actual" = "$expected" ] \
        || fail "SHA256 mismatch for $(basename "$file"). expected=${expected} actual=${actual}"
}

expand_main_dir_placeholder() {
    printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_config_path_or_default() {
    local query="$1"
    local default_value="$2"
    local value=""

    if [ -x "$YQ_BIN" ] && [ -f "$CONFIG_FILE" ]; then
        value="$("$YQ_BIN" -r "$query // \"\"" "$CONFIG_FILE" 2>/dev/null || true)"
    fi

    if [ -z "$value" ] || [ "$value" = "null" ]; then
        value="$default_value"
    fi

    expand_main_dir_placeholder "$value"
}

# The headers a source build needs, and the package that provides each one.
require_build_dependencies() {
    local missing=()
    command -v gcc >/dev/null 2>&1 || command -v cc >/dev/null 2>&1 || missing+=("a C compiler")
    command -v make >/dev/null 2>&1 || missing+=("make")
    [ -e /usr/include/event2/event.h ] || missing+=("libevent headers")
    [ -e /usr/include/openssl/ssl.h ] || missing+=("OpenSSL headers")
    [ -e /usr/include/zlib.h ] || missing+=("zlib headers")
    [ "${#missing[@]}" -eq 0 ] && return 0

    echo "Error: building Tor needs: ${missing[*]}." >&2
    if command -v apt-get >/dev/null 2>&1; then
        echo "Install them with: sudo apt-get install -y build-essential libevent-dev libssl-dev zlib1g-dev" >&2
    elif command -v dnf >/dev/null 2>&1; then
        echo "Install them with: sudo dnf install -y gcc make libevent-devel openssl-devel zlib-devel" >&2
    fi
    exit 1
}

TOR_RUNTIME_ROOT="$(read_config_path_or_default '.dependencies.tor.RUNTIME_ROOT' "$TARGET_DIR/runtime/tor")"
install_dir="${TOR_RUNTIME_ROOT}/${TOR_VERSION}"
work_dir="$(mktemp -d /tmp/nodo-tor.XXXXXX)"
staging="${install_dir}.partial"
trap 'rm -rf "$work_dir" "$staging"' EXIT

mkdir -p "$TOR_RUNTIME_ROOT"
rm -rf "$staging"
mkdir -p "$staging/tor"

case "$(uname -m)" in
    x86_64|amd64)
        archive="tor-expert-bundle-linux-x86_64-${TOR_BROWSER_VERSION}.tar.gz"
        echo "Installing Tor ${TOR_VERSION} (Tor Expert Bundle ${TOR_BROWSER_VERSION})..."
        download_file "https://dist.torproject.org/torbrowser/${TOR_BROWSER_VERSION}/${archive}" "$work_dir/$archive"
        verify_sha256 "$work_dir/$archive" "$TOR_BUNDLE_SHA256_linux_x86_64"
        tar -xzf "$work_dir/$archive" -C "$work_dir"
        # Only tor and its libraries: the pluggable transports are bridges, and nodo uses none.
        for f in tor libcrypto.so.3 libevent-2.1.so.7 libssl.so.3; do
            cp -a "$work_dir/tor/$f" "$staging/tor/$f"
        done
        ;;
    aarch64|arm64)
        require_build_dependencies
        archive="tor-${TOR_VERSION}.tar.gz"
        echo "Building Tor ${TOR_VERSION} from source (no prebuilt Linux/arm64 bundle exists)..."
        download_file "https://dist.torproject.org/${archive}" "$work_dir/$archive"
        verify_sha256 "$work_dir/$archive" "$TOR_SOURCE_SHA256"
        tar -xzf "$work_dir/$archive" -C "$work_dir"
        (
            cd "$work_dir/tor-${TOR_VERSION}"
            ./configure --prefix="$work_dir/prefix" \
                --disable-asciidoc --disable-manpage --disable-html-manual \
                --disable-unittests --disable-module-relay --disable-module-dirauth \
                --disable-system-torrc >"$work_dir/configure.log" 2>&1 \
                || { tail -n 30 "$work_dir/configure.log" >&2; exit 1; }
            make -j"$(nproc)" >"$work_dir/make.log" 2>&1 \
                || { tail -n 30 "$work_dir/make.log" >&2; exit 1; }
            make install >/dev/null
        ) || fail "Building Tor failed."
        cp -a "$work_dir/prefix/bin/tor" "$staging/tor/tor"
        ;;
    *)
        fail "Unsupported architecture $(uname -m)."
        ;;
esac

chmod 755 "$staging/tor/tor"
LD_LIBRARY_PATH="$staging/tor${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$staging/tor/tor" --version >/dev/null \
    || fail "The installed tor does not run on this host."

rm -rf "$install_dir"
mv "$staging" "$install_dir"
ln -sfn "$install_dir" "${TOR_RUNTIME_ROOT}/current"
test -x "${TOR_RUNTIME_ROOT}/current/tor/tor" || fail "Tor not found at ${TOR_RUNTIME_ROOT}/current/tor/tor"

echo "Tor ${TOR_VERSION} installed at ${TOR_RUNTIME_ROOT}/current"
