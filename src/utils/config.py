import os
import yaml
import hashlib
import subprocess
import time
from functools import reduce
from typing import Final, Dict, Callable, Any
import docker as docker_lib
from mnemonic import Mnemonic
from protos import celaut_pb2
from src.utils.singleton import Singleton
from src.utils.network import get_free_port

class ConfigManager(metaclass=Singleton):
    """
    Manages application configuration using a YAML file.
    It loads the configuration, handles nested structures, processes dynamic values
    (like 'auto' for ports or path interpolation), and provides a simple
    interface to access configuration values.
    """
    def __init__(self, config_path="config.yaml", cache_duration=1.0):
        """
        Initializes the ConfigManager.
        Args:
            config_path (str): The path to the YAML configuration file.
            cache_duration (float): Time in seconds to cache config before reloading from disk.
        """
        self.config_path = config_path
        self.cache_duration = cache_duration
        self._config = {}
        self._last_load_time = 0
        self.load_config()

    def _get_nested(self, data: Dict, keys: list) -> Any:
        """Access a nested dictionary value using a list of keys."""
        try:
            return reduce(lambda d, k: d[k], keys, data)
        except (KeyError, TypeError):
            return None

    def _set_nested(self, data: Dict, keys: list, value: Any):
        """Set a nested dictionary value using a list of keys."""
        for key in keys[:-1]:
            data = data.setdefault(key, {})
        data[keys[-1]] = value

    def _should_reload_config(self) -> bool:
        """
        Determines if the configuration should be reloaded from disk.
        Returns True if more than cache_duration seconds have passed since last load.
        """
        return (time.time() - self._last_load_time) > self.cache_duration

    def load_config(self):
        """Loads the YAML file, processes dynamic values, and interpolates paths."""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found at: {self.config_path}")

        with open(self.config_path, 'r') as f:
            self._config = yaml.safe_load(f)

        self._process_dynamic_values()
        self._interpolate_paths(self._config)
        self._last_load_time = time.time()

    def save_config(self):
        """Saves the current configuration back to the YAML file."""
        # Note: Using standard yaml.dump will lose comments and formatting.
        # For preserving them, consider using a library like `ruamel.yaml`.
        with open(self.config_path, 'w') as f:
            yaml.dump(self._config, f, indent=2, default_flow_style=False)

        try:
            os.chmod(self.config_path, 0o666)  # To allow sudo nodo update and still be writable
        except Exception as e:
            pass
        
        # Update the last load time since we just saved (config is fresh)
        self._last_load_time = time.time()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value.
        Nested values can be accessed using dot notation (e.g., 'docker.DOCKER_CLIENT_TIMEOUT').
        Also allows top-level lookups (e.g., 'DOCKER_CLIENT_TIMEOUT').
        
        Reloads config from disk only if cache_duration has expired.
        """
        # Reload config only if cache has expired
        if self._should_reload_config():
            self.load_config()

        # 1. Try to get the value using nested key resolution
        value = self._get_nested(self._config, key.split('.'))
        if value is not None:
            return value

        # 2. If the key is not nested, check top-level sections
        if '.' not in key:
            # 2a. Direct top-level key
            if key in self._config:
                return self._config[key]
            # 2b. Search within each top-level section (if it's a dict)
            for section in self._config.values():
                if isinstance(section, dict) and key in section:
                    return section[key]

        # 3. Return the default if the key wasn't found
        return default

    def set(self, key: str, value: Any):
        """
        Sets a configuration value and saves it to the file.
        Nested values can be accessed using dot notation.
        """
        self._set_nested(self._config, key.split('.'), value)
        self.save_config()

    def force_reload(self):
        """
        Forces a reload of the configuration from disk, ignoring cache.
        Useful when you know the file has been modified externally.
        """
        self.load_config()

    def _interpolate_paths(self, data: Any, context: Dict = None):
        """
        Recursively interpolates path variables like ${VAR_NAME}.
        """
        if context is None:
            # Create a flat context for easy lookups, e.g., {'main.MAIN_DIR': '/nodo'}
            context = self._flatten_dict(self._config)

        if isinstance(data, dict):
            for key, value in data.items():
                data[key] = self._interpolate_paths(value, context)
        elif isinstance(data, list):
            return [self._interpolate_paths(item, context) for item in data]
        elif isinstance(data, str):
            # Simple interpolation: find all ${...} placeholders
            for placeholder in [p for p in data.split('${') if '}' in p]:
                var_name = placeholder.split('}')[0]
                if var_name in context:
                    data = data.replace(f'${{{var_name}}}', str(context[var_name]))
        return data

    def _flatten_dict(self, d: Dict, parent_key: str = '', sep: str = '.') -> Dict:
        """Flattens a nested dictionary."""
        items = []
        for k, v in d.items():
            new_key = parent_key + sep + k if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def _process_dynamic_values(self):
        """Handles special 'auto' values for dynamic configuration."""
        # Handle auto gateway port
        if self.get('network.GATEWAY_PORT') == 'auto':
            port = get_free_port(open_port=True)
            self._set_nested(self._config, ['network', 'GATEWAY_PORT'], port)
            print(f"Dynamically assigned Gateway Port: {port}")

        # Handle auto mnemonics in ledgers
        if 'ledgers' in self._config and isinstance(self._config['ledgers'], list):
            for i, ledger in enumerate(self._config['ledgers']):
                if ledger.get('WALLET_MNEMONIC') == 'auto':
                    mnemonic = Mnemonic("english").generate(strength=128)
                    self._config['ledgers'][i]['WALLET_MNEMONIC'] = mnemonic
                    print(f"Generated new mnemonic for ledger '{ledger.get('name', i)}'")
                if ledger.get('AUXILIARY_MNEMONIC') == 'auto':
                    mnemonic = Mnemonic("english").generate(strength=128)
                    self._config['ledgers'][i]['AUXILIARY_MNEMONIC'] = mnemonic
                    print(f"Generated new auxiliary mnemonic for ledger '{ledger.get('name', i)}'")
        
        # Save changes made by dynamic processing
        self.save_config()


# ---------------------------------------------------------------------------
# ----------- SCRIPT INITIALIZATION AND CONSTANT DEFINITION -----------------
# ---------------------------------------------------------------------------

# 1. Instantiate the ConfigManager
# This will load 'config.yaml', process dynamic values, and interpolate paths.
try:
    config = ConfigManager()
except FileNotFoundError as e:
    print(f"ERROR: {e}")
    # Handle error appropriately, maybe exit or create a default config
    exit(1)


# 2. Define constants and dynamic variables that are not part of the config file

# -- HASH CONSTANTS --
SHAKE_256_ID: Final[bytes] = bytes.fromhex("46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f")
SHA3_256_ID: Final[bytes] = bytes.fromhex("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a")

# -- HASH FUNCTIONS --
SHAKE_256: Callable[[bytes], bytes] = lambda value: b"" if value is None else hashlib.shake_256(value).digest(32)
SHA3_256: Callable[[bytes], bytes] = lambda value: b"" if value is None else hashlib.sha3_256(value).digest()

HASH_FUNCTIONS: Final[Dict[bytes, Callable[[bytes], bytes]]] = {
    SHA3_256_ID: SHA3_256,
    SHAKE_256_ID: SHAKE_256
}

# -- DYNAMICALLY CONSTRUCTED VARIABLES --
# These variables are derived from the configuration but are not simple values.

# Supported Architectures
PACKER_SUPPORTED_ARCHITECTURES = []
if config.get('packer.ARM_PACKER_SUPPORT'):
    PACKER_SUPPORTED_ARCHITECTURES.append(['linux/arm64', 'arm64', 'arm_64', 'aarch64'])
if config.get('packer.X86_PACKER_SUPPORT'):
    PACKER_SUPPORTED_ARCHITECTURES.append(['linux/amd64', 'x86_64', 'amd64'])

SUPPORTED_ARCHITECTURES = []
if config.get('builder.ARM_SUPPORT'):
    SUPPORTED_ARCHITECTURES.append(['linux/arm64', 'arm64', 'arm_64', 'aarch64'])
if config.get('builder.X86_SUPPORT'):
    SUPPORTED_ARCHITECTURES.append(['linux/amd64', 'x86_64', 'amd64'])

# Docker client factory
try:
    DOCKER_COMMAND = subprocess.check_output(["which", "docker"]).strip().decode("utf-8")
except (subprocess.CalledProcessError, FileNotFoundError):
    DOCKER_COMMAND = "/usr/bin/docker" # Fallback
    print("Warning: 'docker' command not found in PATH. Using fallback.")

DOCKER_CLIENT = lambda: docker_lib.from_env(
    timeout=config.get("docker.DOCKER_CLIENT_TIMEOUT", 480),
    max_pool_size=config.get("docker.DOCKER_MAX_CONNECTIONS", 1000)
)

# Default System Resources for Manager
DEFAULT_SYSTEM_RESOURCES: celaut_pb2.Sysresources = celaut_pb2.Sysresources(
    mem_limit=50 * pow(10, 6),
)