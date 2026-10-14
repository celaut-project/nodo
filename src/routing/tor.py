"""The Tor backend: ``.onion`` peers, reached through a Tor process nodo manages.

nodo runs its own ``tor`` (installed by ``bash/install_tor.sh``) with a private
data directory under ``STORAGE/tor``. It opens an ``HTTPTunnelPort`` on loopback and
nothing else: no SOCKS port, no control port, no listener a peer could reach.

If the node announces an onion address (``routing.tor.ANNOUNCE``), tor also hosts the
gateway as an onion service. It forwards to a loopback-only gRPC server; no port of the
host is opened to the Internet.

A call to a ``.onion`` host goes to that port as an HTTP ``CONNECT`` with the host
*name*, so the name is resolved inside Tor and never by the local resolver.
"""

import json
import os
import re
import socket
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from src.routing import onion_key
from src.routing.base import RoutingBackend, RoutingUnavailable
from src.routing.policy import hidden_mode
from src.routing.tor_install import (
    TorInstallError,
    ensure_tor_installed,
    managed_binary,
    tor_installed,
)
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.network import get_free_port

env_manager = ConfigManager()

TOR_TAG = "tor-onion-v3"

# Vanguards-lite protects the onion services of a node from guard discovery. It
# arrived in 0.4.7 and is on by default from 0.4.8 (the plan's phase 2 depends on it).
MIN_TOR_VERSION = (0, 4, 8)

BOOTSTRAP_TIMEOUT_S = 180
_ONION_V3 = re.compile(r"^[a-z2-7]{56}\.onion$")
_VERSION = re.compile(r"Tor version (\d+)\.(\d+)\.(\d+)")


def is_onion_host(host: str) -> bool:
    return str(host or "").strip().lower().rstrip(".").endswith(".onion")


def is_onion_v3(host: str) -> bool:
    return bool(_ONION_V3.match(str(host or "").strip().lower()))


def tor_mode() -> str:
    """``"on"``, ``"off"`` or ``"auto"``, from ``routing.tor.ENABLED``.

    ``auto`` uses Tor if nodo's copy is already installed and never installs it.
    ``true`` installs it on first start. Hidden mode is always ``on``.
    """
    value = env_manager.get("routing.tor.ENABLED", "auto")
    if hidden_mode():
        return "on"  # A hidden node has no other way to call or be called.
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "auto"


def tor_binary() -> Path:
    override = env_manager.get("routing.tor.BINARY", "")
    return Path(str(override)) if override else managed_binary()


def status_file() -> Path:
    return Path(str(env_manager.get("STORAGE"))) / "tor" / "state.json"


class TorBackend(RoutingBackend):
    name = "tor"
    tag = TOR_TAG
    # A circuit to an onion service takes seconds, and a first one tens of them.
    preflight_timeout_s = 60.0
    probe_timeout_s = 30.0
    probe_ttl_open_s = 300.0
    probe_ttl_closed_s = 60.0

    def __init__(self):
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._http_port: Optional[int] = None
        self._state = "stopped"
        self._detail = ""
        self._onion: Optional[Tuple[str, bytes, int, int]] = None

    # --- the onion service -----------------------------------------------------

    def configure_onion(self, mnemonic: str, local_port: int, virtual_port: int) -> str:
        """Host the gateway as an onion service. Call before :meth:`start`.

        Tor accepts on ``virtual_port`` of the onion name and forwards to
        ``127.0.0.1:local_port``, where the onion-only gRPC server listens. Returns the
        ``.onion`` name, which the mnemonic fixes: it is known before Tor is up.
        """
        seed = onion_key.derive_seed(mnemonic)
        host = onion_key.onion_host(onion_key.public_key(seed))
        with self._lock:
            self._onion = (host, seed, local_port, virtual_port)
        return host

    def onion_host(self) -> Optional[str]:
        """The onion name this node hosts its gateway at, or None if it hosts none."""
        with self._lock:
            return self._onion[0] if self._onion else None

    def onion_port(self) -> Optional[int]:
        """The port peers dial on the onion name."""
        with self._lock:
            return self._onion[3] if self._onion else None

    def _set(self, state: str, detail: str = "") -> None:
        """Change state, and leave it on disk for ``nodo tor status`` (another process)."""
        self._state, self._detail = state, detail
        try:
            path = status_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "state": state, "detail": detail, "pid": os.getpid(),
                "onion": self.onion_host(),
            }))
        except Exception:
            pass

    # --- what the registry and the CLI ask -------------------------------------

    def handles_host(self, host: str) -> bool:
        return is_onion_host(host)

    def valid_host(self, host: str) -> bool:
        return is_onion_v3(host)

    def available(self) -> bool:
        with self._lock:
            return (
                self._state == "ready"
                and self._http_port is not None
                and (self._process is None or self._process.poll() is None)
            )

    def status(self) -> Tuple[str, str]:
        """``(state, detail)``: disabled, not-installed, starting, ready, failed, stopped."""
        with self._lock:
            return self._state, self._detail

    # --- the connection --------------------------------------------------------

    def open_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        if not self.available():
            raise RoutingUnavailable(
                f"{host}:{port} is reached through Tor, and Tor is not running on this node."
            )
        sock = socket.create_connection(("127.0.0.1", self._http_port), timeout=timeout)
        try:
            authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            sock.sendall(
                f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
            )
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ConnectionError(f"Tor closed the connection to {authority}.")
                reply += chunk
                if len(reply) > 8192:
                    raise ConnectionError("Tor sent an oversized reply to CONNECT.")
            status = reply.split(b"\r\n", 1)[0].decode("ascii", "replace")
            if status.split(" ")[1:2] != ["200"]:
                raise ConnectionError(f"Tor could not reach {authority}: {status}")
            return sock
        except BaseException:
            sock.close()
            raise

    def grpc_channel_options(self, host: str) -> List[Tuple[str, object]]:
        if not self.available():
            raise RoutingUnavailable(
                f"{host} is reached through Tor, and Tor is not running on this node."
            )
        return [("grpc.http_proxy", f"http://127.0.0.1:{self._http_port}")]

    # --- the process -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._state in ("starting", "ready"):
                return
            mode = tor_mode()
            if mode == "off":
                self._set("disabled", "routing.tor.ENABLED is false.")
                return
            binary = tor_binary()
            if not tor_installed(binary):
                if mode == "auto":
                    self._set("not-installed", "Run `nodo tor install`, or set routing.tor.ENABLED: true.")
                    return
                if binary != managed_binary():
                    self._fail(f"routing.tor.BINARY {binary} is not an executable file.")
                    return
                try:
                    ensure_tor_installed()
                except TorInstallError as e:
                    self._fail(str(e))
                    return
            self._set("starting")
        try:
            self._launch(binary)
        except Exception as e:  # never stop the node over a backend that failed to start
            with self._lock:
                self._fail(f"{type(e).__name__}: {e}")

    def _fail(self, detail: str) -> None:
        self._set("failed", detail)
        log.LOGGER(f"[TOR] {detail}")

    def _launch(self, binary: Path) -> None:
        env = os.environ.copy()
        if binary == managed_binary():
            env["LD_LIBRARY_PATH"] = os.pathsep.join(
                filter(None, [str(binary.parent), env.get("LD_LIBRARY_PATH", "")])
            )
        version = _tor_version(binary, env)
        if version is None or version < MIN_TOR_VERSION:
            wanted = ".".join(map(str, MIN_TOR_VERSION))
            found = ".".join(map(str, version)) if version else "unknown"
            with self._lock:
                self._fail(f"{binary} is Tor {found}; nodo needs {wanted} or later.")
            return

        storage = Path(str(env_manager.get("STORAGE")))
        tor_dir = storage / "tor"
        data_dir = tor_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        port = get_free_port()
        torrc = tor_dir / "torrc"
        lines = [
            f"DataDirectory {data_dir}",
            "SocksPort 0",
            f"HTTPTunnelPort 127.0.0.1:{port}",
            "ClientOnly 1",
            "AvoidDiskWrites 1",
            "Log notice stdout",
        ]
        with self._lock:
            onion = self._onion
        if onion:
            _, seed, local_port, virtual_port = onion
            _, service_dir = onion_key.write_hidden_service_dir(tor_dir / "gateway", seed)
            lines += [
                f"HiddenServiceDir {service_dir}",
                "HiddenServiceVersion 3",
                f"HiddenServicePort {virtual_port} 127.0.0.1:{local_port}",
            ]
        torrc.write_text("\n".join(lines) + "\n")
        os.chmod(torrc, 0o600)

        process = subprocess.Popen(
            [str(binary), "-f", str(torrc)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            start_new_session=True,
        )
        with self._lock:
            self._process, self._http_port = process, port
        threading.Thread(target=self._watch, args=(process,), daemon=True).start()
        timer = threading.Timer(BOOTSTRAP_TIMEOUT_S, self._bootstrap_timeout, args=(process,))
        timer.daemon = True  # a pending timeout must never keep the node from exiting
        timer.start()

    def _watch(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            if "Bootstrapped 100%" in line:
                with self._lock:
                    if self._process is process:
                        self._set("ready")
                log.LOGGER("[TOR] Bootstrapped. .onion peers are reachable.")
        process.wait()
        with self._lock:
            if self._process is process and self._state != "stopped":
                self._fail(f"tor exited with status {process.returncode}.")

    def _bootstrap_timeout(self, process: subprocess.Popen) -> None:
        with self._lock:
            if self._process is process and self._state == "starting":
                self._fail(f"tor did not bootstrap in {BOOTSTRAP_TIMEOUT_S} seconds.")

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._http_port = None
            self._set("stopped")
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _tor_version(binary: Path, env: dict) -> Optional[Tuple[int, int, int]]:
    try:
        out = subprocess.run(
            [str(binary), "--version"], env=env, capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(out)
    return tuple(int(g) for g in match.groups()) if match else None
