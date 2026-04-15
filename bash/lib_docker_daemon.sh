#!/bin/bash
# lib_docker_daemon.sh
# Shared helpers for nodo's isolated Docker daemon scripts.

set -e

ensure_daemon_config() {
    local config_dir="$1"
    local daemon_json="${config_dir}/daemon.json"

    if [ ! -f "${daemon_json}" ]; then
        cat > "${daemon_json}" <<EOF
{
    "storage-driver": "overlay2",
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    },
    "ipv6": false,
    "default-cgroupns-mode": "private"
}
EOF
    fi

    # Remove legacy/unsupported keys that break Docker 24.x
    # apparmor-profile is not a valid daemon.json option
    if grep -q '"apparmor-profile"' "${daemon_json}"; then
        sed -i '/"apparmor-profile"/d' "${daemon_json}"
    fi
    # Avoid duplicate keys when we pass flags for these values
    sed -i '/"data-root"/d' "${daemon_json}"
    sed -i '/"exec-root"/d' "${daemon_json}"
}
