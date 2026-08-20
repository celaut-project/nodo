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
#   cpio/gzip                 pack the Cloud Hypervisor initramfs (busybox, its
#                             only binary, comes from the Nodo release)
#   curl/ca-certificates/git  fetch runtimes and sources
#   procps/iproute/ping/iptables/e2fsprogs  execute preflight (src/virtualizers/ch/execute.py)
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
