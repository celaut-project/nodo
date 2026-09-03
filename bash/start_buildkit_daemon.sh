#!/bin/bash
# start_buildkit_daemon.sh
# Starts nodo's ROOTLESS BuildKit builder, used by the local packer.
#
# Runs entirely as the invoking user: no sudo here, and none in
# stop_buildkit_daemon.sh either. That is the whole point of the rootless
# builder — a daemon we own is a daemon we can always stop.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/lib_rootless.sh
. "${SCRIPT_DIR}/lib_rootless.sh"

if [ -z "$1" ]; then
    echo "Error: TARGET_DIR is not provided."
    exit 1
fi

TARGET_DIR="$1"
BIN_DIR="${TARGET_DIR}/bin"
BUILDKIT_DIR="${TARGET_DIR}/buildkit"
BUILDKIT_ROOT="${BUILDKIT_DIR}/data"
BUILDKIT_SOCKET="${BUILDKIT_DIR}/buildkitd.sock"
BUILDKIT_PID_FILE="${BUILDKIT_DIR}/buildkitd.pid"
BUILDKIT_LOG_FILE="${BUILDKIT_DIR}/buildkitd.log"
BUILDKIT_RUN_DIR="${BUILDKIT_DIR}/run"

expand_main_dir_placeholder() {
    printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_config_path_or_default() {
    local query="$1"
    local default_value="$2"
    local yq_bin="${TARGET_DIR}/bin/yq"
    local config_file="${TARGET_DIR}/config.yaml"
    local value=""

    if [ -x "$yq_bin" ] && [ -f "$config_file" ]; then
        value="$("$yq_bin" -r "$query // \"\"" "$config_file" 2>/dev/null || true)"
    fi

    if [ -z "$value" ] || [ "$value" = "null" ]; then
        value="$default_value"
    fi

    expand_main_dir_placeholder "$value"
}

BUILDKITD_BIN="$(read_config_path_or_default '.dependencies.buildkit.DAEMON_BIN' "${BIN_DIR}/buildkitd")"
BUILDCTL_BIN="$(read_config_path_or_default '.dependencies.buildkit.BIN' "${BIN_DIR}/buildctl")"

if [ ! -x "${BUILDKITD_BIN}" ]; then
    echo "Error: buildkitd not found at ${BUILDKITD_BIN}. Run the installer (bash/install_buildkit.sh)."
    exit 1
fi

if [ ! -x "${BUILDCTL_BIN}" ]; then
    echo "Error: buildctl not found at ${BUILDCTL_BIN}. Run the installer (bash/install_buildkit.sh)."
    exit 1
fi

# The build sandbox needs a multi-uid user namespace. Fail here with the exact
# missing pieces rather than letting a Dockerfile die confusingly on `apt-get`.
MISSING="$(rootless_prereqs_missing "${BIN_DIR}" || true)"
if [ -n "${MISSING}" ]; then
    echo "Error: the rootless builder is missing host prerequisites: $(echo ${MISSING} | tr '\n' ' ')"
    echo "Run the installer to provision them (it needs sudo once, and never again):"
    echo "    bash \"${SCRIPT_DIR}/install_buildkit.sh\" \"${TARGET_DIR}\""
    exit 1
fi

ROOTLESSKIT_BIN="$(resolve_rootlesskit "${BIN_DIR}")"

mkdir -p "${BUILDKIT_ROOT}" "${BUILDKIT_RUN_DIR}"
chmod 700 "${BUILDKIT_RUN_DIR}"

# Already running and answering? Idempotent success.
if [ -S "${BUILDKIT_SOCKET}" ] && "${BUILDCTL_BIN}" --addr "unix://${BUILDKIT_SOCKET}" debug workers >/dev/null 2>&1; then
    echo "Nodo BuildKit builder already running and responsive."
    exit 0
fi

# Orphan sweep. Unlike the old root dockerd, this daemon belongs to us, so a
# wedged one can simply be cleared instead of blocking the pack forever.
RUNNING_PIDS="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
if [ -n "${RUNNING_PIDS}" ]; then
    echo "Clearing an unresponsive BuildKit builder on ${BUILDKIT_ROOT} (PID(s): ${RUNNING_PIDS})..."
    for p in ${RUNNING_PIDS}; do kill "${p}" 2>/dev/null || true; done
    for i in $(seq 1 10); do
        still="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
        [ -z "${still}" ] && break
        sleep 1
    done
    still="$(buildkitd_pids "${BUILDKIT_ROOT}" || true)"
    if [ -n "${still}" ]; then
        for p in ${still}; do kill -9 "${p}" 2>/dev/null || true; done
        sleep 1
    fi
fi
rm -f "${BUILDKIT_SOCKET}" "${BUILDKIT_PID_FILE}"

echo "Starting rootless BuildKit builder for nodo..."
echo "  Socket: ${BUILDKIT_SOCKET}"
echo "  Root: ${BUILDKIT_ROOT}"
echo "  Log file: ${BUILDKIT_LOG_FILE}"
echo "  rootlesskit: ${ROOTLESSKIT_BIN}"

dump_daemon_log() {
    echo "----- Last lines of ${BUILDKIT_LOG_FILE} -----"
    tail -n 40 "${BUILDKIT_LOG_FILE}" 2>/dev/null || true
    echo "----------------------------------------------"
}

# --net=host keeps the build on the host's network (what the old buildx builder
# used `--network host` for: reachable DNS and registries) and avoids needing
# --copy-up. buildkit-runc and the CNI helpers live next to buildkitd, so PATH
# must include BIN_DIR. XDG_RUNTIME_DIR is pinned inside BUILDKIT_DIR to keep
# the worker's runtime state isolated to the node.
nohup env \
    PATH="$(dirname "${BUILDKITD_BIN}"):${PATH}" \
    XDG_RUNTIME_DIR="${BUILDKIT_RUN_DIR}" \
    HOME="${HOME}" \
    "${ROOTLESSKIT_BIN}" \
        --net=host \
        "${BUILDKITD_BIN}" \
            --rootless \
            --root "${BUILDKIT_ROOT}" \
            --addr "unix://${BUILDKIT_SOCKET}" \
            --oci-worker-net host \
    > "${BUILDKIT_LOG_FILE}" 2>&1 &

DAEMON_PID=$!
echo "${DAEMON_PID}" > "${BUILDKIT_PID_FILE}"

echo "Waiting for the BuildKit builder to start..."
for i in $(seq 1 30); do
    if ! kill -0 "${DAEMON_PID}" 2>/dev/null; then
        echo "Error: the BuildKit builder exited during startup."
        dump_daemon_log
        rm -f "${BUILDKIT_PID_FILE}"
        exit 1
    fi

    if [ -S "${BUILDKIT_SOCKET}" ] \
        && "${BUILDCTL_BIN}" --addr "unix://${BUILDKIT_SOCKET}" debug workers >/dev/null 2>&1; then
        echo "Nodo BuildKit builder started successfully (PID: ${DAEMON_PID})."
        exit 0
    fi
    sleep 1
done

echo "Error: the BuildKit builder did not become responsive within 30 seconds."
dump_daemon_log
exit 1
