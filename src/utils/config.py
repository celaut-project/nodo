import copy
import os
import subprocess
import tempfile
import threading
import time
from functools import reduce
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from mnemonic import Mnemonic

from src.utils.network import get_free_port
from src.utils.singleton import Singleton


def to_yaml_safe(value: Any) -> Any:
    """Normalize a value into plain YAML-serializable Python types.

    Values that reach the config sometimes come from the Java bridge (jpype),
    e.g. a ``java.lang.String`` returned by an Appkit ``.toString()`` call. Such
    objects are ``str`` subclasses that PyYAML does not recognize, so ``yaml.dump``
    would persist them as ``!!python/object:jpype._jstring.java.lang.String`` —
    which then fails to load. Coercing every value to an exact builtin type here
    keeps ``config.yaml`` clean regardless of where a value originated.
    """
    if value is None or type(value) in (bool, int, float, str):
        return value
    # Normalize subclasses (jpype JString/JInt/JDouble, Decimal, etc.).
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {to_yaml_safe(k): to_yaml_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_yaml_safe(item) for item in value]
    # Any other foreign object (e.g. an unmapped Java type) becomes its string form.
    return str(value)


def _construct_foreign_as_string(loader: "yaml.Loader", tag_suffix: str, node: yaml.Node) -> Any:
    """Recover a foreign ``!!python/object:*`` node as its underlying string.

    Lets a config that was written with an embedded Java object (before the
    coercion above existed) still load, so nodo can rewrite it cleanly.
    """
    try:
        if isinstance(node, yaml.MappingNode):
            mapping = loader.construct_mapping(node, deep=True)
            for key in ("_jstr", "value", "data"):
                if mapping.get(key) is not None:
                    return str(mapping[key])
            for candidate in mapping.values():
                if candidate is not None:
                    return str(candidate)
            return ""
        if isinstance(node, yaml.ScalarNode):
            return str(loader.construct_scalar(node))
        if isinstance(node, yaml.SequenceNode):
            return [str(item) for item in loader.construct_sequence(node, deep=True)]
    except Exception:
        return ""
    return ""


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that degrades foreign python/object tags to strings."""


_TolerantLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/object", _construct_foreign_as_string
)


# config.yaml is shared between the long-running `nodo serve` daemon and the
# short-lived CLI/TUI processes. A CLI write (e.g. storing a freshly submitted
# REPUTATION_PROOF_ID) must become visible to the daemon, which would otherwise
# keep serving the config it read at boot — and, worse, overwrite the file with
# it on the next `set()`. Re-stat the file at most once every
# _RELOAD_CHECK_INTERVAL seconds and reload when it changed on disk.
_RELOAD_CHECK_INTERVAL = 5.0


class ConfigManager(metaclass=Singleton):
    """
    Manages application configuration using a YAML file.
    It loads the configuration, handles nested structures, processes dynamic values
    (like 'auto' for ports or path interpolation), and provides a simple
    interface to access configuration values.
    """

    def __init__(self, config_path: str = "config.yaml", log: Callable[[str], None] = lambda msg: None):
        self.config_path = config_path
        self._config: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._config_mtime_ns: Optional[int] = None
        self._last_reload_check: float = 0.0
        self.log = log

    def _get_nested(self, data: Dict[str, Any], keys: List[str]) -> Any:
        """Access a nested dictionary value using a list of keys."""
        try:
            return reduce(lambda d, k: d[k], keys, data)
        except (KeyError, TypeError):
            return None

    def _set_nested(self, data: Dict[str, Any], keys: List[str], value: Any):
        """Set a nested dictionary value using a list of keys."""
        for key in keys[:-1]:
            data = data.setdefault(key, {})
        data[keys[-1]] = value

    def ensure_loaded(self):
        """Lazily load configuration, refreshing it if another process rewrote it."""
        with self._lock:
            if self._loaded:
                self._reload_if_file_changed()
                return
            self.load_config()

    def _current_mtime_ns(self) -> Optional[int]:
        try:
            return os.stat(self.config_path).st_mtime_ns
        except OSError:
            return None

    def _reload_if_file_changed(self, force_check: bool = False):
        """Reload config.yaml when it changed on disk since we last read it.

        Assumes the caller holds the lock and the config is already loaded. Pass
        force_check to skip the _RELOAD_CHECK_INTERVAL debounce, which `set()`
        does so a write always lands on top of the newest file contents.
        """
        now = time.monotonic()
        if not force_check and now - self._last_reload_check < _RELOAD_CHECK_INTERVAL:
            return
        self._last_reload_check = now

        mtime = self._current_mtime_ns()
        if mtime is None or mtime == self._config_mtime_ns:
            return

        previous = self._config
        try:
            self.load_config(force_reload=True)
            if not self._config and previous:
                raise ValueError("the file parsed as empty")
        except Exception as e:
            # An unreadable or half-written file must never wipe a working config.
            self._config = previous
            # Don't retry (and re-log) until the file changes again.
            self._config_mtime_ns = mtime
            self.log(f"Could not reload {self.config_path}, keeping the loaded config: {e}")
            return

        self.log(f"Reloaded {self.config_path} after an external change.")

    def _allow_gateway_port_with_iptables(self, port: int):
        rule = [
            "-p",
            "tcp",
            "--dport",
            str(port),
            "-j",
            "ACCEPT",
            "-m",
            "comment",
            "--comment",
            "nodo;gateway;auto_port",
        ]
        try:
            check_result = subprocess.run(
                ["iptables", "-C", "INPUT", *rule],
                capture_output=True,
                text=True,
                check=False,
            )
            if check_result.returncode == 0:
                return

            subprocess.run(
                ["iptables", "-I", "INPUT", *rule],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise Exception(
                f"Error attempting to open port {port} in the firewall (iptables): {e.stderr}"
            )
        except FileNotFoundError:
            raise Exception(
                "iptables command not found. Ensure iptables is installed if you intend to open ports."
            )

    def load_config(self, force_reload: bool = False):
        """
        Loads the YAML file, processes dynamic values, and interpolates paths.
        Idempotent unless force_reload=True.
        """
        with self._lock:
            if self._loaded and not force_reload:
                return

            if not os.path.exists(self.config_path):
                raise FileNotFoundError(f"Configuration file not found at: {self.config_path}")

            with open(self.config_path, "r") as f:
                raw = f.read()

            recovered = False
            try:
                self._config = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                # A previous version may have persisted a foreign (Java) object.
                # Recover it as plain strings, then force a clean rewrite below.
                self._config = yaml.load(raw, Loader=_TolerantLoader) or {}
                recovered = True
                self.log(
                    "config.yaml contained a non-native (Java) value; recovered it "
                    "and rewriting the file cleanly."
                )

            original_config = copy.deepcopy(self._config)

            # Fail fast on removed keys from the pre-single-wallet layout. There is no
            # migration: a stale config must be updated by hand.
            from src.utils.config_validation import _find_removed_keys, ConfigValidationError
            _removed = _find_removed_keys(self._config)
            if _removed:
                raise ConfigValidationError(
                    "Removed configuration keys are still present (no migration is "
                    "provided; update the config manually): "
                    + ", ".join(sorted(_removed))
                )

            # Process dynamic values.
            gateway_port = self._get_nested(self._config, ["network", "GATEWAY_PORT"])
            if gateway_port == "auto":
                free_port_ranges = self._get_nested(self._config, ["network", "FREE_PORTS_RANGE"]) or []
                port = get_free_port(free_port_ranges=free_port_ranges)
                if port and os.geteuid() == 0:
                    self._allow_gateway_port_with_iptables(port=port)
                self._set_nested(self._config, ["network", "GATEWAY_PORT"], port)
                self.log(f"Dynamically assigned Gateway Port: {port}")

            # Handle auto mnemonic for the single wallet of each ledger.
            # ``ledgers`` is a mapping keyed by ledger name (e.g. ``ledgers.ergo``);
            # each ledger owns exactly ONE wallet (WALLET_MNEMONIC). There is no
            # auxiliary/receiver wallet.
            ledgers = self._config.get("ledgers")
            if isinstance(ledgers, dict):
                for name, ledger in ledgers.items():
                    if not isinstance(ledger, dict):
                        continue
                    if ledger.get("WALLET_MNEMONIC") == "auto":
                        mnemonic = Mnemonic("english").generate(strength=128)
                        ledger["WALLET_MNEMONIC"] = mnemonic
                        self.log(f"Generated new mnemonic for ledger '{name}'")

            # Node identity keypair (see src/reputation_system/node_identity.py): a node
            # needs a stable identity from first boot even with no ledger configured, so
            # this gets the same "auto" treatment as a ledger wallet. When a ledger wallet
            # IS configured, node_identity prefers it instead, so the two coincide and a
            # reputation proof published later reuses the node's own identity for free.
            network_section = self._config.get("network")
            if isinstance(network_section, dict) and network_section.get("NODE_MNEMONIC") == "auto":
                network_section["NODE_MNEMONIC"] = Mnemonic("english").generate(strength=128)
                self.log("Generated new node identity mnemonic")

            config_changed = self._config != original_config

            # Interpolate paths after dynamic values are processed.
            self._interpolate_paths(self._config)

            # Save if dynamic processing made changes or we recovered a bad file.
            if config_changed or recovered:
                self.log("Dynamic values were processed, saving configuration...")
                self._save_config_unlocked()

            self._config_mtime_ns = self._current_mtime_ns()
            self._last_reload_check = time.monotonic()
            self._loaded = True

    def _save_config_unlocked(self):
        """Internal save method without locking (assumes caller holds lock)."""
        # Coerce to native types first, then use safe_dump so a foreign object can
        # never again be persisted as a `!!python/object:...` tag.
        safe_config = to_yaml_safe(self._config)
        # Write through a temporary file so a concurrent reader (another nodo
        # process reloading on mtime) never sees a truncated config.yaml. Falls
        # back to an in-place write where the directory isn't writable.
        if not self._atomic_write(safe_config):
            with open(self.config_path, "w") as f:
                yaml.safe_dump(safe_config, f, indent=2, default_flow_style=False)
            self._chmod_config()

        self._config_mtime_ns = self._current_mtime_ns()

    def _atomic_write(self, safe_config: Dict[str, Any]) -> bool:
        # Resolve symlinks: replacing the link itself would detach the config
        # from wherever the install actually keeps it.
        target = os.path.realpath(self.config_path)
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".config-", suffix=".yaml", dir=os.path.dirname(target)
            )
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(safe_config, f, indent=2, default_flow_style=False)
            os.chmod(tmp_path, 0o666)  # To allow sudo nodo update and still be writable
            os.replace(tmp_path, target)
            return True
        except OSError:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return False

    def _chmod_config(self):
        try:
            os.chmod(self.config_path, 0o666)  # To allow sudo nodo update and still be writable
        except Exception:
            pass

    def save_config(self):
        """Saves the current configuration back to the YAML file."""
        with self._lock:
            self.ensure_loaded()
            self._save_config_unlocked()

    def validate_ergo(self, payments_enabled: bool = True, reputation_enabled: bool = True) -> None:
        """Validate the Ergo ledger config; raises ConfigValidationError when invalid."""
        from src.utils.config_validation import validate_ergo_config
        with self._lock:
            self.ensure_loaded()
            validate_ergo_config(
                self._config,
                payments_enabled=payments_enabled,
                reputation_enabled=reputation_enabled,
            )

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value.
        Nested values can be accessed using dot notation (e.g., 'virtualizers.ch.NETWORK_BRIDGE_NAME').
        Also allows top-level lookups (e.g., 'GATEWAY_PORT').
        """
        with self._lock:
            self.ensure_loaded()

            value = self._get_nested(self._config, key.split("."))
            if value is not None:
                return value

            if "." not in key:
                if key in self._config:
                    return self._config[key]
                for section in self._config.values():
                    if isinstance(section, dict) and key in section:
                        return section[key]

            return default

    def set(self, key: str, value: Any):
        """
        Sets a configuration value and saves it to the file.
        Nested values can be accessed using dot notation.
        """
        with self._lock:
            self.ensure_loaded()
            # Saving rewrites the whole file, so start from what is on disk right
            # now: otherwise a stale in-memory copy would silently revert keys
            # another process wrote (e.g. the daemon dropping the CLI's
            # REPUTATION_PROOF_ID while updating ledgers.ergo.NODE_URL).
            self._reload_if_file_changed(force_check=True)
            # Normalize now so in-memory reads also get a native value, not a
            # Java/foreign object that would later poison the YAML file.
            self._set_nested(self._config, key.split("."), to_yaml_safe(value))
            self._save_config_unlocked()

    def _interpolate_paths(self, data: Any, context: Optional[Dict[str, Any]] = None):
        """Recursively interpolates path variables like ${VAR_NAME}."""

        def _flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
            items: List[Tuple[str, Any]] = []
            for k, v in d.items():
                new_key = parent_key + sep + k if parent_key else k
                if isinstance(v, dict):
                    items.extend(_flatten_dict(v, new_key, sep=sep).items())
                else:
                    items.append((new_key, v))
            return dict(items)

        if context is None:
            context = _flatten_dict(self._config)

        if isinstance(data, dict):
            for key, value in data.items():
                data[key] = self._interpolate_paths(value, context)
        elif isinstance(data, list):
            return [self._interpolate_paths(item, context) for item in data]
        elif isinstance(data, str):
            for placeholder in [p for p in data.split("${") if "}" in p]:
                var_name = placeholder.split("}")[0]
                if var_name in context:
                    data = data.replace(f"${{{var_name}}}", str(context[var_name]))
        return data
