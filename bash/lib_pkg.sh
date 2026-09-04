# Package-manager abstraction for the setup scripts. Sourced, not executed.
#
# Nodo brings its own runtimes (Python, Java, yq, cloud-hypervisor, guest kernel),
# so all it needs from the host distro is a short list of tools. This file maps
# those generic names to per-distro package names, so `install.sh` works on any
# Linux instead of only on Debian derivatives.
#
# Tested on Ubuntu 22.04/24.04 (apt) and Fedora Asahi Remix 44 (dnf). Adding a
# distro means adding one branch per alias below — nothing else in the installer
# knows what a package is called.

PKG_MGR=""

if ! declare -F fail >/dev/null 2>&1; then
    fail() {
        echo "Error: $1" >&2
        exit 1
    }
fi

detect_pkg_mgr() {
    if command -v apt-get >/dev/null 2>&1; then
        PKG_MGR="apt"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MGR="dnf"
    else
        fail "No supported package manager found (looked for apt-get and dnf).
Install these tools by hand and re-run: a C compiler + make, clang, cpio, gzip,
zip, curl, ca-certificates, git, procps, iproute, ping, iptables, e2fsprogs, and
an en_US.UTF-8 locale. See docs/INSTALL.md."
    fi
}

# Generic alias -> package name(s) for the detected manager.
pkg_for() {
    local alias_name="$1"

    case "$alias_name" in
        # A compiler is needed because psutil has no linux-aarch64 wheel and pip
        # builds it from source; clang because the portable CPython's sysconfig
        # hardcodes CC=clang (see PYTHON_BUILD_TAG in the setup scripts).
        compiler)   case "$PKG_MGR" in apt) echo "build-essential" ;; dnf) echo "gcc make" ;; esac ;;
        clang)      echo "clang" ;;
        procps)     case "$PKG_MGR" in apt) echo "procps" ;;        dnf) echo "procps-ng" ;; esac ;;
        iproute)    case "$PKG_MGR" in apt) echo "iproute2" ;;      dnf) echo "iproute" ;; esac ;;
        ping)       case "$PKG_MGR" in apt) echo "iputils-ping" ;;  dnf) echo "iputils" ;; esac ;;
        iptables)   case "$PKG_MGR" in apt) echo "iptables" ;;      dnf) echo "iptables-nft" ;; esac ;;
        locale)     case "$PKG_MGR" in apt) echo "locales" ;;       dnf) echo "glibc-langpack-en" ;; esac ;;
        cpio|gzip|zip|curl|git|e2fsprogs|ca-certificates) echo "$alias_name" ;;
        *)          fail "Unknown package alias '${alias_name}'." ;;
    esac
}

# What the node needs from the host, by role:
#   compiler/clang            build psutil during `pip install`
#   cpio/gzip                 inspect the Cloud Hypervisor initramfs before each
#                             launch (src/virtualizers/ch/execute.py); the image
#                             itself is built by CI and comes from the Nodo release
#   curl/ca-certificates/git  fetch runtimes and sources
#   procps/iproute/ping/iptables/e2fsprogs  execute preflight (src/virtualizers/microvm/network.py)
#   zip                       packing
NODO_HOST_PACKAGE_ALIASES=(
    compiler
    clang
    cpio
    gzip
    zip
    curl
    ca-certificates
    git
    procps
    iproute
    ping
    iptables
    e2fsprogs
    locale
)

pkg_update() {
    case "$PKG_MGR" in
        apt)
            apt-get update -o Acquire::AllowInsecureRepositories=true -o Acquire::Check-Valid-Until=false \
                || {
                    echo "apt-get update failed; clearing locks and retrying..."
                    rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock*
                    dpkg --configure -a || true
                    apt-get update
                }
            ;;
        dnf)
            dnf makecache --refresh
            ;;
    esac
}

pkg_install_host_dependencies() {
    local alias_name
    local packages=()

    for alias_name in "${NODO_HOST_PACKAGE_ALIASES[@]}"; do
        # shellcheck disable=SC2207
        packages+=($(pkg_for "$alias_name"))
    done

    echo "Installing host dependencies with ${PKG_MGR}: ${packages[*]}"
    case "$PKG_MGR" in
        apt)
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}" \
                || fail "Failed to install host dependencies. See the apt output above."
            ;;
        dnf)
            dnf install -y "${packages[@]}" \
                || fail "Failed to install host dependencies. See the dnf output above."
            ;;
    esac
}

# qemu-system for an architecture this host does NOT run natively, so the node can
# execute foreign-arch services under TCG (see `virtualizers.qemu` in config.yaml).
# Debian ships the aarch64 emulator inside qemu-system-arm; Fedora splits it per
# target. An empty result means "no package known here", which is not an error.
pkg_for_qemu_system() {
    local arch_tag="$1"

    case "$arch_tag" in
        linux/arm64) case "$PKG_MGR" in apt) echo "qemu-system-arm" ;;    dnf) echo "qemu-system-aarch64" ;; esac ;;
        linux/amd64) case "$PKG_MGR" in apt) echo "qemu-system-x86" ;;    dnf) echo "qemu-system-x86" ;; esac ;;
    esac
}

install_foreign_arch_emulator() {
    # Best effort ON PURPOSE. The emulator is a large, optional package, and a host
    # that cannot install it must still finish installing nodo. The node decides
    # which architectures to advertise by looking for this binary and the guest
    # assets on disk (src/utils/architectures.py), so failing here costs capacity
    # -- the node serves only its own arch -- and never a broken install or a node
    # announcing an arch it cannot boot.
    local arch_tag="$1"
    local packages

    # shellcheck disable=SC2207
    packages=($(pkg_for_qemu_system "$arch_tag"))
    if [ "${#packages[@]}" -eq 0 ]; then
        echo "No qemu-system package known for ${arch_tag} on ${PKG_MGR}; skipping emulated ${arch_tag} support."
        return 0
    fi

    echo "Installing ${packages[*]} so this node can execute ${arch_tag} services under emulation..."
    case "$PKG_MGR" in
        apt)
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}" \
                || echo "Warning: could not install ${packages[*]}. This node will serve only its own architecture." >&2
            ;;
        dnf)
            dnf install -y "${packages[@]}" \
                || echo "Warning: could not install ${packages[*]}. This node will serve only its own architecture." >&2
            ;;
    esac
}

ensure_utf8_locale() {
    echo "Ensuring UTF-8 locale support..."
    # Debian generates locales on demand; on Fedora (and most others) the
    # langpack package is all there is, so there is nothing to generate.
    if command -v locale-gen >/dev/null 2>&1; then
        locale-gen en_US.UTF-8 >/dev/null || true
    fi
    if command -v update-locale >/dev/null 2>&1; then
        update-locale LANG=en_US.UTF-8 || true
    fi
}

# The setup scripts install these, but a host may also arrive with them missing
# from a manual install. Checking here turns a confusing mid-install failure into
# one message naming the missing tool.
verify_host_tools() {
    local tool
    local missing=()

    for tool in cpio gzip zip curl git ip ping iptables debugfs; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done

    if [ "${#missing[@]}" -gt 0 ]; then
        fail "Missing host tools after dependency install: ${missing[*]}"
    fi
}

# The unit's Group is distro-specific: `sudo` on Debian/Ubuntu, `wheel` on
# Fedora/RHEL. systemd refuses to load a unit whose Group does not resolve, so it
# can never be hardcoded. Lives here because install.sh and the setup scripts both
# render bash/nodo.service.template and both need the same answer; the third
# renderer is _resolve_admin_group() in src/commands/doctor.py, kept in sync by
# tests/test_installer_distro_support.py.
resolve_admin_group() {
    local group
    for group in sudo wheel; do
        if getent group "$group" >/dev/null 2>&1; then
            printf '%s\n' "$group"
            return 0
        fi
    done
    printf 'root\n'
}
