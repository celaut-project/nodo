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
#
# Takes the builder's bin dir, because rootlesskit is NOT required to be on PATH:
# on distros that carry no rootlesskit package (Arch, Alpine) the installer drops
# the upstream static binary in MAIN_DIR/bin, which is only on PATH when nodo is
# invoked through its wrapper. Asking `command -v` there reported the dependency
# missing immediately after installing it, so the installer failed its own
# post-provision recheck and `start` refused to launch. Resolve it the same way
# the caller will actually invoke it instead.
rootless_prereqs_missing() {
    local bin_dir="${1:-}"
    command -v newuidmap >/dev/null 2>&1 || echo "newuidmap"
    command -v newgidmap >/dev/null 2>&1 || echo "newgidmap"
    [ -x "$(resolve_rootlesskit "$bin_dir")" ] || echo "rootlesskit"
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
    local bin_dir="${1:-}"
    if [ -x /usr/bin/rootlesskit ]; then
        printf '%s' /usr/bin/rootlesskit
        return 0
    fi
    command -v rootlesskit 2>/dev/null && return 0
    # No bin dir to fall back on: report a path that cannot exist rather than a
    # bare "/rootlesskit", so callers testing -x get a clean "missing".
    [ -n "$bin_dir" ] || { printf '%s' "rootlesskit"; return 0; }
    printf '%s' "${bin_dir}/rootlesskit"
}
