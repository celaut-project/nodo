import copy
import os
import subprocess
import threading
from functools import reduce
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from mnemonic import Mnemonic

from src.utils.network import get_free_port
from src.utils.singleton import Singleton


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
        """Lazily load configuration once per process."""
        with self._lock:
            if self._loaded:
                return
            self.load_config()

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
                self._config = yaml.safe_load(f) or {}

            original_config = copy.deepcopy(self._config)

            # Process dynamic values.
            gateway_port = self._get_nested(self._config, ["network", "GATEWAY_PORT"])
            if gateway_port == "auto":
                free_port_ranges = self._get_nested(self._config, ["network", "FREE_PORTS_RANGE"]) or []
                port = get_free_port(free_port_ranges=free_port_ranges)
                if port and os.geteuid() == 0:
                    self._allow_gateway_port_with_iptables(port=port)
                self._set_nested(self._config, ["network", "GATEWAY_PORT"], port)
                self.log(f"Dynamically assigned Gateway Port: {port}")

            # Handle auto mnemonics in ledgers.
            ledgers = self._config.get("ledgers")
            if isinstance(ledgers, list):
                for i, ledger in enumerate(ledgers):
                    if ledger.get("WALLET_MNEMONIC") == "auto":
                        mnemonic = Mnemonic("english").generate(strength=128)
                        ledgers[i]["WALLET_MNEMONIC"] = mnemonic
                        self.log(f"Generated new mnemonic for ledger '{ledger.get('name', i)}'")
                    if ledger.get("AUXILIARY_MNEMONIC") == "auto":
                        mnemonic = Mnemonic("english").generate(strength=128)
                        ledgers[i]["AUXILIARY_MNEMONIC"] = mnemonic
                        self.log(f"Generated new auxiliary mnemonic for ledger '{ledger.get('name', i)}'")

            config_changed = self._config != original_config

            # Interpolate paths after dynamic values are processed.
            self._interpolate_paths(self._config)

            # Save if dynamic processing made changes.
            if config_changed:
                self.log("Dynamic values were processed, saving configuration...")
                self._save_config_unlocked()

            self._loaded = True

    def _save_config_unlocked(self):
        """Internal save method without locking (assumes caller holds lock)."""
        with open(self.config_path, "w") as f:
            yaml.dump(self._config, f, indent=2, default_flow_style=False)

        try:
            os.chmod(self.config_path, 0o666)  # To allow sudo nodo update and still be writable
        except Exception:
            pass

    def save_config(self):
        """Saves the current configuration back to the YAML file."""
        with self._lock:
            self.ensure_loaded()
            self._save_config_unlocked()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value.
        Nested values can be accessed using dot notation (e.g., 'virtualizers.docker.DOCKER_CLIENT_TIMEOUT').
        Also allows top-level lookups (e.g., 'DOCKER_CLIENT_TIMEOUT').
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
            self._set_nested(self._config, key.split("."), value)
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
