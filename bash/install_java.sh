#!/bin/bash

set -euo pipefail

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Error: You must pass the project root directory as the first argument."
    exit 1
fi

TARGET_DIR="$(cd "$TARGET_DIR" >/dev/null 2>&1 && pwd)"
CONFIG_FILE="$TARGET_DIR/config.yaml"

case "$(uname -m)" in
    x86_64|amd64)
        JRE_DIST="OpenJDK21U-jre_x64_linux_hotspot_21.0.8_9.tar.gz"
        YQ_URL="https://github.com/mikefarah/yq/releases/download/v4.44.3/yq_linux_amd64"
        ;;
    aarch64|arm64)
        JRE_DIST="OpenJDK21U-jre_aarch64_linux_hotspot_21.0.8_9.tar.gz"
        YQ_URL="https://github.com/mikefarah/yq/releases/download/v4.44.3/yq_linux_arm64"
        ;;
    *)
        echo "Error: Unsupported architecture $(uname -m)."
        exit 1
        ;;
esac

JRE_VERSION="21.0.8_9"
JRE_RELEASE_TAG="jdk-21.0.8%2B9"
JRE_BASE_URL="https://github.com/adoptium/temurin21-binaries/releases/download/${JRE_RELEASE_TAG}"
JRE_URL="${JRE_BASE_URL}/${JRE_DIST}"
JRE_SHA_URL="${JRE_URL}.sha256.txt"
RUNTIME_DIR="$TARGET_DIR/runtime"
JAVA_RUNTIME_ROOT_DEFAULT="$RUNTIME_DIR/java"
YQ_BIN_DEFAULT="$TARGET_DIR/bin/yq"
YQ_BIN="$YQ_BIN_DEFAULT"

fail() {
    echo "Error: $1"
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

extract_archive() {
    local archive="$1"
    local destination="$2"
    local tmp_dir

    tmp_dir="$(mktemp -d)"
    tar -xzf "$archive" -C "$tmp_dir"

    shopt -s nullglob
    local entries=("$tmp_dir"/*)
    shopt -u nullglob

    rm -rf "$destination"
    mkdir -p "$destination"

    if [ "${#entries[@]}" -eq 1 ] && [ -d "${entries[0]}" ]; then
        cp -a "${entries[0]}/." "$destination/"
    else
        cp -a "$tmp_dir/." "$destination/"
    fi

    rm -rf "$tmp_dir"
}

install_local_yq() {
    mkdir -p "$(dirname "$YQ_BIN_DEFAULT")"
    download_file "$YQ_URL" "$YQ_BIN_DEFAULT"
    chmod +x "$YQ_BIN_DEFAULT"
    YQ_BIN="$YQ_BIN_DEFAULT"
}

if [ ! -x "$YQ_BIN" ]; then
    install_local_yq
fi

JAVA_RUNTIME_ROOT="$(read_config_path_or_default '.dependencies.java.RUNTIME_ROOT' "$JAVA_RUNTIME_ROOT_DEFAULT")"

archive="$(mktemp /tmp/nodo-jre.XXXXXX.tar.gz)"
checksum="$(mktemp /tmp/nodo-jre-sha.XXXXXX)"
install_dir="${JAVA_RUNTIME_ROOT}/${JRE_VERSION}"

mkdir -p "$JAVA_RUNTIME_ROOT"

echo "Installing portable Temurin JRE ${JRE_VERSION}..."
download_file "$JRE_URL" "$archive"
download_file "$JRE_SHA_URL" "$checksum"

expected="$(awk '{print $1}' "$checksum" | head -n1)"
[ -n "$expected" ] || fail "Failed to read expected SHA256 from ${JRE_SHA_URL}"

actual="$(sha256sum "$archive" | awk '{print $1}')"
[ "$actual" = "$expected" ] || fail "SHA256 mismatch for ${JRE_DIST}. expected=${expected} actual=${actual}"

extract_archive "$archive" "$install_dir"

ln -sfn "$install_dir" "${JAVA_RUNTIME_ROOT}/current"
test -x "${JAVA_RUNTIME_ROOT}/current/bin/java" \
    || fail "Portable Java not found at ${JAVA_RUNTIME_ROOT}/current/bin/java"

rm -f "$archive" "$checksum"

echo "Java installed at ${JAVA_RUNTIME_ROOT}/current"
