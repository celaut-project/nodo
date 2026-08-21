#!/bin/bash
# lib_rootless.sh
# Shared helpers for nodo's ROOTLESS BuildKit builder (the local packer's toolchain).
#
# The builder runs as the invoking user, never as root, so nodo can always start
# and stop it on its own: `kill` never hits EPERM, the socket belongs to us, and
# no step of a `nodo pack` needs sudo. Nothing in this file is privileged — the
# one-time host prerequisites are handled by install_buildkit.sh.

set -e

# Echo the PIDs holding a given BuildKit --root: both the buildkitd worker and
# the rootlesskit parent that wrapped it (its cmdline carries the same flags).
# Matches only our builder, never a system-wide buildkitd. Tolerant of no
# matches under `set -e`: the trailing awk always exits 0.
buildkitd_pids() {
    local root="$1"
    [ -z "$root" ] && return 0
    pgrep -af buildkitd 2>/dev/null \
        | grep -F -- "--root ${root}" \
        | awk '{print $1}'
}

# Echo one line per missing rootless prerequisite; empty output means ready.
# newuidmap/newgidmap and the /etc/sub[ug]id ranges are what let the build map
# more than a single uid inside the user namespace — without them most
# Dockerfiles fail on `apt-get install`/`chown`. rootlesskit is what actually
# creates the namespace (and, on AppArmor distros, what is allowed to).
rootless_prereqs_missing() {
    command -v newuidmap >/dev/null 2>&1 || echo "newuidmap"
    command -v newgidmap >/dev/null 2>&1 || echo "newgidmap"
    command -v rootlesskit >/dev/null 2>&1 || echo "rootlesskit"
    grep -q "^$(id -un):" /etc/subuid 2>/dev/null || echo "subuid"
    grep -q "^$(id -un):" /etc/subgid 2>/dev/null || echo "subgid"
}

# Resolve the rootlesskit to use. A distro-packaged /usr/bin/rootlesskit is
# preferred over nodo's own copy: on Ubuntu 24.04+ the kernel refuses
# unprivileged user namespaces to unconfined binaries
# (kernel.apparmor_restrict_unprivileged_userns=1) and the shipped
# /etc/apparmor.d/rootlesskit profile grants the exemption BY PATH — a copy
# under MAIN_DIR/bin would be denied.
resolve_rootlesskit() {
    local bin_dir="$1"
    if [ -x /usr/bin/rootlesskit ]; then
        printf '%s' /usr/bin/rootlesskit
        return 0
    fi
    command -v rootlesskit 2>/dev/null || printf '%s' "${bin_dir}/rootlesskit"
}
