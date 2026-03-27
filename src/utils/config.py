import os
import yaml
import hashlib
import subprocess
import time
import copy
import threading
from pathlib import Path
from functools import reduce
from typing import Final, Dict, Callable, Any
import docker as docker_lib
from mnemonic import Mnemonic
from protos import celaut_pb2
from src.utils.singleton import Singleton
from src.utils.network import get_free_port
from src.utils.logger import LOGGER as log

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
        self._lock = threading.RLock()  # Reentrant lock for nested calls
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
        with self._lock:
            if not os.path.exists(self.config_path):
                raise FileNotFoundError(f"Configuration file not found at: {self.config_path}")

            with open(self.config_path, 'r') as f:
                self._config = yaml.safe_load(f)

            # Track if we need to save changes - use deep copy for nested structures
            original_config = copy.deepcopy(self._config)
            
            # Process dynamic values
            self._process_dynamic_values()
            config_changed = (self._config != original_config)
                
            # Interpolate paths
            self._interpolate_paths(self._config)
            self._last_load_time = time.time()
            
            # Save if dynamic processing made changes
            if config_changed:
                log("Dynamic values were processed, saving configuration...")
                self._save_config_unlocked()  # Use internal method to avoid double locking

    def _save_config_unlocked(self):
        """Internal save method without locking (assumes caller holds lock)."""
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

    def save_config(self):
        """Saves the current configuration back to the YAML file."""
        with self._lock:
            self._save_config_unlocked()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value.
        Nested values can be accessed using dot notation (e.g., 'virtualizers.docker.DOCKER_CLIENT_TIMEOUT').
        Also allows top-level lookups (e.g., 'DOCKER_CLIENT_TIMEOUT').
        
        Reloads config from disk only if cache_duration has expired.
        """
        with self._lock:
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
        with self._lock:
            self._set_nested(self._config, key.split('.'), value)
            self._save_config_unlocked()

    def force_reload(self):
        """
        Forces a reload of the configuration from disk, ignoring cache.
        Useful when you know the file has been modified externally.
        """
        with self._lock:
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
        # Handle auto gateway port - use direct config access to avoid recursion
        gateway_port = self._get_nested(self._config, ['network', 'GATEWAY_PORT'])
        if gateway_port == 'auto':
            port = get_free_port()
            if port and os.geteuid() == 0:
                try:
                    subprocess.run(
                        ["ufw", "allow", f"{port}/tcp"],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError as e:
                    raise Exception(
                        f"Error attempting to open port {port} in the firewall (ufw): {e.stderr}"
                    )
                except FileNotFoundError:
                    raise Exception("ufw command not found. Ensure ufw is installed if you intend to open ports.")
            self._set_nested(self._config, ['network', 'GATEWAY_PORT'], port)
            log(f"Dynamically assigned Gateway Port: {port}")

        # Handle auto mnemonics in ledgers
        if 'ledgers' in self._config and isinstance(self._config['ledgers'], list):
            for i, ledger in enumerate(self._config['ledgers']):
                if ledger.get('WALLET_MNEMONIC') == 'auto':
                    mnemonic = Mnemonic("english").generate(strength=128)
                    self._config['ledgers'][i]['WALLET_MNEMONIC'] = mnemonic
                    log(f"Generated new mnemonic for ledger '{ledger.get('name', i)}'")
                if ledger.get('AUXILIARY_MNEMONIC') == 'auto':
                    mnemonic = Mnemonic("english").generate(strength=128)
                    self._config['ledgers'][i]['AUXILIARY_MNEMONIC'] = mnemonic
                    log(f"Generated new auxiliary mnemonic for ledger '{ledger.get('name', i)}'")
        
        # DON'T save here to avoid recursion - let load_config handle the timing
        # The save will happen when set() is called from outside


# ---------------------------------------------------------------------------
# ----------- SCRIPT INITIALIZATION AND CONSTANT DEFINITION -----------------
# ---------------------------------------------------------------------------

# 1. Instantiate the ConfigManager
# This will load 'config.yaml', process dynamic values, and interpolate paths.
try:
    config = ConfigManager()
except FileNotFoundError as e:
    log(f"ERROR: {e}")
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

# Docker client factory - uses isolated Docker daemon
# Get the private Docker socket path from config
# Base paths for the isolated Docker installation (inside nodo directory)
_main_dir = config.get("main.MAIN_DIR")
NODO_ROOT = Path(_main_dir).expanduser().resolve() if _main_dir else Path(__file__).resolve().parents[2]
BIN_DIR = NODO_ROOT / "bin"
PLUGIN_DIR = NODO_ROOT / "libexec" / "docker" / "cli-plugins"

DOCKER_BIN = str(BIN_DIR / "docker")
DOCKERD_BIN = str(BIN_DIR / "dockerd")

# Private Docker socket (defaults to nodo's docker/ dir if not set)
DOCKER_SOCKET = config.get("virtualizers.docker.DOCKER_SOCKET") or str(NODO_ROOT / "docker" / "docker.sock")

# Validate isolated binaries and plugin
if not os.path.isfile(DOCKER_BIN):
    raise RuntimeError(f"Cliente Docker de Nodo no encontrado en {DOCKER_BIN}. Ejecuta el instalador.")
if not os.path.isfile(str(PLUGIN_DIR / "docker-buildx")):
    raise RuntimeError(f"Plugin buildx no encontrado en {PLUGIN_DIR}. Ejecuta el instalador.")

# Isolated environment for ALL Docker CLI calls
DOCKER_ENV = os.environ.copy()
DOCKER_ENV.update({
    "DOCKER_CLI_PLUGINS_DIR": str(PLUGIN_DIR),
    "DOCKER_API_VERSION": "1.43",
    "DOCKER_HOST": f"unix://{DOCKER_SOCKET}",
    "PATH": f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
    "DOCKER_CONFIG": str(NODO_ROOT / "libexec" / "docker")
})

# Base Docker command as a list (safer than strings + shlex)
# DOCKER_COMMAND = [DOCKER_BIN, "-H", f"unix://{DOCKER_SOCKET}"]
DOCKER_COMMAND = [DOCKER_BIN]
                
# DOCKER_CLIENT factory - connects to the isolated Docker daemon
def _ensure_docker_daemon_running():
    """
    Ensures the isolated Docker daemon is running.
    If the socket doesn't exist, attempts to start the daemon.
    """
    socket_path = DOCKER_SOCKET
    if not socket_path:
        return True
    
    # Check if socket exists and is accessible
    if os.path.exists(socket_path):
        return True
    
    # Socket doesn't exist, try to start the daemon
    main_dir = config.get("main.MAIN_DIR")
    start_script = os.path.join(main_dir, "bash", "start_docker_daemon.sh")
    
    if os.path.exists(start_script):
        log(f"Starting isolated Docker daemon for nodo...")
        try:
            result = subprocess.run(
                ["/bin/bash", start_script, main_dir],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                log("Isolated Docker daemon started successfully.")
                return True
            else:
                log(f"Warning: Failed to start isolated Docker daemon: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            log("Warning: Timeout starting isolated Docker daemon.")
            return False
        except Exception as e:
            log(f"Warning: Error starting isolated Docker daemon: {e}")
            return False
    else:
        log(f"Warning: Docker daemon start script not found at {start_script}")
        return False

def _create_docker_client():
    """Creates a Docker client connected to nodo's isolated daemon."""
    socket_path = DOCKER_SOCKET
    # Ensure the daemon is running before connecting
    _ensure_docker_daemon_running()
    return docker_lib.DockerClient(
        base_url=f"unix://{socket_path}",
        timeout=config.get("virtualizers.docker.DOCKER_CLIENT_TIMEOUT", 480),
        max_pool_size=config.get("virtualizers.docker.DOCKER_MAX_CONNECTIONS", 1000)
    )

DOCKER_CLIENT = _create_docker_client

# Default System Resources for Manager
DEFAULT_SYSTEM_RESOURCES: celaut_pb2.Sysresources = celaut_pb2.Sysresources(
    mem_limit=50 * pow(10, 6),
)
