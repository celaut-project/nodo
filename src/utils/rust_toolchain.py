"""Where nodo's Rust toolchain lives, and how `nodo tui` finds something to run.

Python and Java are installed *under the installation root* -- `runtime/python`,
`runtime/java`, each with a `current` symlink and a `dependencies.*.RUNTIME_ROOT`
key to relocate it. Rust was the exception: the setup scripts piped
`sh.rustup.rs` with its stock defaults, which land in `$HOME/.cargo` and
`$HOME/.rustup`, and `source`d `~/.cargo/env` in the setup script's own subshell.
Nothing of that survives into the process that later runs `nodo tui`, so a node
with a perfectly good toolchain reported "Installing Rust (Cargo)..." and
installed it again -- into whichever `$HOME` happened to be current, which under
`sudo nodo tui` is not even the same directory twice (issue #375).

The fix is not a better search. It is having one place to look: `RUNTIME_ROOT`
under the installation root, `RUSTUP_HOME` and `CARGO_HOME` inside it, and fixed
paths derived from that for `cargo` and `rustc`. Nothing here reads `$PATH` or
`$HOME`, so nothing here can be wrong about which toolchain the node owns. An
existing `~/.cargo` is left exactly where it is and is never consulted: it
belongs to the operator, not to this node.

The second half of the same issue is that a node should not need a Rust
toolchain at all. `nodo tui` is one binary; CI builds it per target
(`.github/workflows/tui-release.yml`) and publishes it with the `rustc -vV` host
triple beside it. A prebuilt whose marker matches this host is executed
directly, and only a host with no usable prebuilt ever compiles anything.

Deliberately stdlib-only apart from a *lazy* `ConfigManager` read, so `nodo
doctor` and the tests can import it on a checkout with no `config.yaml`.
"""
import glob
import os
import platform
import subprocess
from typing import Dict, List, Optional, Tuple

from src.utils.arch_guard import host_arch_tag


#: Config keys, in the same shape as their `python`/`java` siblings.
RUNTIME_ROOT_KEY = "dependencies.rust.RUNTIME_ROOT"
INSTALL_TOOLCHAIN_KEY = "dependencies.rust.INSTALL_TOOLCHAIN"

#: Where rustup comes from. One constant, shared by the setup scripts (which
#: hardcode the same URL) and by the in-process install below.
RUSTUP_URL = "https://sh.rustup.rs"

#: The TUI crate, relative to the installation root, and cargo's own release
#: output path inside it. CI publishes into exactly this path so that a shipped
#: binary and a locally built one are the same file to everything downstream --
#: there is no second location to keep in sync, and `cargo build --release`
#: overwrites the published binary rather than being shadowed by it.
TUI_CRATE_RELPATH = os.path.join("src", "commands", "tui")
TUI_BINARY_RELPATH = os.path.join(TUI_CRATE_RELPATH, "target", "release", "tui")

#: The marker that says which host the binary next to it was built for. A file
#: rather than a probe: running an aarch64 ELF on x86_64 fails with `Exec format
#: error`, which the operator sees as `nodo tui` doing nothing at all.
TUI_MARKER_SUFFIX = ".host-triple"

#: Rust's architecture names for the canonical tags nodo already has a table for.
#: Derived from `arch_guard` rather than re-deriving from `platform.machine()`:
#: the alias table is what decides what this host *is*, and a second copy of it
#: could only ever disagree.
_RUST_ARCH_BY_TAG = {
    "linux/amd64": "x86_64",
    "linux/arm64": "aarch64",
}


def _config_get(key: str) -> Optional[str]:
    """One config read that never raises. A missing/broken config means "default"."""
    try:
        from src.utils.config import ConfigManager

        value = ConfigManager().get(key)
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def installation_root(main_dir: Optional[str] = None) -> str:
    """The installation root every path below hangs off."""
    if main_dir:
        return str(main_dir)
    configured = _config_get("main.MAIN_DIR")
    if configured:
        return configured
    return os.getcwd()


def runtime_root(main_dir: Optional[str] = None) -> str:
    """`dependencies.rust.RUNTIME_ROOT`, or `<MAIN_DIR>/runtime/rust`.

    The same shape as `dependencies.python.RUNTIME_ROOT`, and read the same way,
    so an operator who relocates one runtime relocates this one by the same edit.
    """
    root = installation_root(main_dir)
    configured = _config_get(RUNTIME_ROOT_KEY)
    if configured:
        return _expand_main_dir(configured, root)
    return os.path.join(root, "runtime", "rust")


def _expand_main_dir(value: str, main_dir: str) -> str:
    # ConfigManager already interpolates `${main.MAIN_DIR}` for a loaded config;
    # doing it again costs nothing and keeps this correct when the value came
    # from a file nobody interpolated (a hand-written config, a test).
    return value.replace("${main.MAIN_DIR}", main_dir)


def cargo_home(main_dir: Optional[str] = None) -> str:
    return os.path.join(runtime_root(main_dir), "cargo")


def rustup_home(main_dir: Optional[str] = None) -> str:
    return os.path.join(runtime_root(main_dir), "rustup")


def cargo_bin(main_dir: Optional[str] = None) -> str:
    return os.path.join(cargo_home(main_dir), "bin", "cargo")


def rustc_bin(main_dir: Optional[str] = None) -> str:
    return os.path.join(cargo_home(main_dir), "bin", "rustc")


def toolchain_env(main_dir: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment every cargo/rustup invocation of ours runs in.

    `RUSTUP_HOME` and `CARGO_HOME` are the whole point: rustup honours both, so
    setting them is what keeps the toolchain inside the installation root instead
    of in `$HOME`. `PATH` gets the node's own `cargo/bin` *prepended* because
    cargo shells out to `rustc` by name -- without it a node that owns a
    toolchain would still build with whatever `rustc` the operator's shell has.
    """
    base = dict(os.environ if env is None else env)
    bin_dir = os.path.join(cargo_home(main_dir), "bin")
    base["RUSTUP_HOME"] = rustup_home(main_dir)
    base["CARGO_HOME"] = cargo_home(main_dir)
    base["PATH"] = os.pathsep.join([bin_dir, base.get("PATH", "")]).rstrip(os.pathsep)
    return base


def toolchain_present(main_dir: Optional[str] = None, runner=subprocess.run) -> bool:
    """Whether this node's *own* cargo exists and runs.

    Existence alone was not enough to trust: a half-finished rustup leaves the
    directory behind, and the symptom of trusting it is a build that dies with a
    linker error instead of an install that says the toolchain is broken.
    """
    cargo = cargo_bin(main_dir)
    if not os.path.isfile(cargo) or not os.access(cargo, os.X_OK):
        return False
    try:
        completed = runner(
            [cargo, "--version"],
            env=toolchain_env(main_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return False
    return getattr(completed, "returncode", 1) == 0


def _libc_tag() -> str:
    """`gnu` or `musl`, the way the portable Python download already decides it.

    The Python runtime is pinned to a `-unknown-linux-gnu` build, so glibc is the
    assumption everywhere else in the installer too; this only has to notice the
    case where that assumption is wrong rather than classify every libc. A wrong
    answer here is harmless by construction: it makes the marker disagree with
    the host and the node builds from source.
    """
    try:
        name, _version = platform.libc_ver()
    except Exception:
        name = ""
    if name and "glibc" in name.lower():
        return "gnu"
    if glob.glob("/lib/ld-musl-*.so.1") or os.path.exists("/etc/alpine-release"):
        return "musl"
    return "gnu"


def host_triple() -> Optional[str]:
    """This host's Rust target triple, or None when nodo has no name for it.

    None is a real answer and is treated as "no prebuilt is acceptable here":
    guessing a triple for a machine the arch table does not know would make the
    marker check pass for a binary that cannot run.
    """
    arch = _RUST_ARCH_BY_TAG.get(host_arch_tag() or "")
    if not arch:
        return None
    system = platform.system().lower()
    if system == "linux":
        return f"{arch}-unknown-linux-{_libc_tag()}"
    if system == "darwin":
        return f"{arch}-apple-darwin"
    return None


def prebuilt_paths(main_dir: Optional[str] = None) -> Tuple[str, str]:
    """The prebuilt TUI binary and its host-triple marker."""
    root = installation_root(main_dir)
    binary = os.path.join(root, TUI_BINARY_RELPATH)
    return binary, binary + TUI_MARKER_SUFFIX


def read_marker(marker_path: str) -> Optional[str]:
    """The triple recorded in a marker file, or None when there is not one.

    The file is written from `rustc -vV`'s `host:` line, so the first non-empty
    line is read and anything after it ignored -- a marker that grew a provenance
    comment must not stop matching.
    """
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text:
                    return text
    except OSError:
        return None
    return None


def write_marker(marker_path: str, triple: str) -> None:
    with open(marker_path, "w", encoding="utf-8") as handle:
        handle.write(f"{triple}\n")


def valid_prebuilt(
    main_dir: Optional[str] = None,
    runner=subprocess.run,
) -> Optional[str]:
    """The prebuilt TUI this host may execute, or None.

    Three things are asked, cheapest first, and every one of them has been a real
    failure: the binary is there, its marker names *this* host, and it starts.
    The last one is `tui --version`, which prints a line and exits -- never the
    interface itself, since a check that takes over the terminal is not a check.
    """
    binary, marker = prebuilt_paths(main_dir)
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        return None

    expected = host_triple()
    recorded = read_marker(marker)
    if not expected or not recorded or recorded != expected:
        return None

    try:
        completed = runner(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    return binary


def install_toolchain(
    main_dir: Optional[str] = None,
    runner=subprocess.run,
    printer=print,
) -> bool:
    """Install rustup *into the installation root*, and answer whether it worked.

    `--no-modify-path` is not a detail: the whole bug this replaces came from a
    toolchain that announced itself by editing a shell profile nobody re-read.
    This install touches no rc file and no `$HOME`; it is found again by path,
    because the path is fixed.
    """
    root = runtime_root(main_dir)
    printer(f"Installing Rust into {root} (nodo's own toolchain, not ~/.cargo)...", flush=True)
    try:
        os.makedirs(cargo_home(main_dir), exist_ok=True)
        os.makedirs(rustup_home(main_dir), exist_ok=True)
        completed = runner(
            f"curl --proto '=https' --tlsv1.2 -sSf {RUSTUP_URL} | sh -s -- -y --no-modify-path",
            shell=True,
            env=toolchain_env(main_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as error:
        printer(f"Error installing Rust: {error}", flush=True)
        return False
    if getattr(completed, "returncode", 1) != 0:
        printer("Error installing Rust: rustup exited non-zero.", flush=True)
        return False
    if not toolchain_present(main_dir, runner=runner):
        printer(f"Rust install finished but no usable cargo at {cargo_bin(main_dir)}.", flush=True)
        return False
    printer(f"Rust installed at {root}.", flush=True)
    return True


def ensure_toolchain(
    main_dir: Optional[str] = None,
    runner=subprocess.run,
    printer=print,
) -> bool:
    """This node's cargo, installing it into the installation root if it is absent.

    Replaces `check_rust_installation`'s `rustc --version` on `$PATH`. A
    toolchain the operator installed for themselves is not an answer to "does
    this node own one": it can disappear between two runs of the same command,
    and it was never what the setup script installed.
    """
    if toolchain_present(main_dir, runner=runner):
        return True
    return install_toolchain(main_dir, runner=runner, printer=printer)


def record_host_triple(main_dir: Optional[str] = None, runner=subprocess.run) -> Optional[str]:
    """Write the marker for a binary we just built, from our own `rustc -vV`.

    Taken from the compiler that produced the binary rather than from
    `host_triple()`, because the marker's job is to describe what was *built*,
    and those two differing is precisely the case it exists to catch.
    """
    try:
        completed = runner(
            [rustc_bin(main_dir), "-vV"],
            env=toolchain_env(main_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    stdout = getattr(completed, "stdout", b"") or b""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    triple = None
    for line in stdout.splitlines():
        if line.startswith("host:"):
            triple = line.split(":", 1)[1].strip()
            break
    if not triple:
        return None
    _binary, marker = prebuilt_paths(main_dir)
    try:
        write_marker(marker, triple)
    except OSError:
        return None
    return triple


def build_tui(
    main_dir: Optional[str] = None,
    runner=subprocess.run,
    printer=print,
) -> Optional[str]:
    """Compile the TUI with this node's cargo and return the binary, or None.

    `cargo build --release` followed by executing the binary, rather than `cargo
    run`: `cargo run` re-checks the build graph on every launch, which on a cold
    cache is a compile -- fine at a prompt, and not fine behind a desktop
    shortcut, where it reads as an app that failed to open. Building once and
    recording the marker means the next launch takes the prebuilt branch above
    and starts immediately.
    """
    crate_dir = os.path.join(installation_root(main_dir), TUI_CRATE_RELPATH)
    if not os.path.isdir(crate_dir):
        # A node installed from a release rootfs can legitimately have no crate
        # source. Saying so is the whole value here: without this the caller got
        # `[Errno 2] No such file or directory` naming a path, which reads as a
        # broken install rather than as "this node was shipped a binary and did
        # not get one for its architecture".
        printer(
            f"No TUI source at {crate_dir}, and no prebuilt binary for this host. "
            "Install from a source checkout, or fetch a binary matching this "
            "host's target triple.",
            flush=True,
        )
        return None
    printer("Building the TUI (first run on this host)...", flush=True)
    try:
        completed = runner(
            [cargo_bin(main_dir), "build", "--release"],
            cwd=crate_dir,
            env=toolchain_env(main_dir),
        )
    except (OSError, subprocess.SubprocessError) as error:
        printer(f"Error building the TUI: {error}", flush=True)
        return None
    if getattr(completed, "returncode", 1) != 0:
        printer("Error building the TUI: cargo exited non-zero.", flush=True)
        return None

    binary, _marker = prebuilt_paths(main_dir)
    if not os.path.isfile(binary):
        printer(f"cargo reported success but no binary at {binary}.", flush=True)
        return None
    record_host_triple(main_dir, runner=runner)
    return binary


def resolve_tui(
    main_dir: Optional[str] = None,
    runner=subprocess.run,
    printer=print,
) -> Optional[str]:
    """The TUI binary to execute, in the only order that is safe.

    1. a prebuilt whose marker names this host -- nothing is compiled, and on a
       node installed from a release nothing ever is;
    2. this node's own cargo, if it has one, to build it;
    3. install that cargo into the installation root, then build.

    There is deliberately no fourth step onto a `cargo` from `$PATH`. That is the
    class of bug being removed: which toolchain ran is then a property of the
    invoking shell, and `nodo tui` behaved differently under `sudo` than without
    it for exactly that reason.
    """
    prebuilt = valid_prebuilt(main_dir, runner=runner)
    if prebuilt:
        return prebuilt

    if not ensure_toolchain(main_dir, runner=runner, printer=printer):
        return None

    return build_tui(main_dir, runner=runner, printer=printer)


def tui_command(main_dir: Optional[str] = None, runner=subprocess.run, printer=print) -> Optional[List[str]]:
    """`resolve_tui` as an argv, for a caller that wants to exec it."""
    binary = resolve_tui(main_dir, runner=runner, printer=printer)
    return [binary] if binary else None
