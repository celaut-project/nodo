import os
import shutil
import tempfile

import yaml


_CONFIG_DIRS = []
_DATA_DIR = []


def _data_dir() -> str:
    if not _DATA_DIR:
        _DATA_DIR.append(tempfile.TemporaryDirectory(prefix="nodo-test-data-"))
    return _DATA_DIR[0].name


def load_example_config():
    """Load a temporary example config before importing modules with ConfigManager globals."""
    from src.utils.config import ConfigManager
    from src.utils.singleton import Singleton

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example = os.path.join(root, "config.example.yaml")
    config_dir = tempfile.TemporaryDirectory(prefix="nodo-test-config-")
    _CONFIG_DIRS.append(config_dir)
    config_path = os.path.join(config_dir.name, "config.yaml")
    shutil.copyfile(example, config_path)
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    # One data directory (database, storage) for the whole process, whatever config a
    # module loads. `SQLConnection` keeps the first database it opened for good, and
    # modules capture `DATABASE_FILE` at import, so a data directory per config made
    # every module after the first read and write a different database than the one
    # the code under test was bound to.
    data_dir = _data_dir()
    config.setdefault("main", {})["MAIN_DIR"] = data_dir
    config["main"]["STORAGE"] = os.path.join(data_dir, "storage")
    # The example says `auto`, which a node resolves by opening a port in the host
    # firewall -- as root, once. A test must not need that, and the code under test
    # refuses to run with the port unassigned, so pin one.
    config.setdefault("network", {})["GATEWAY_PORT"] = 58443
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, indent=2)

    Singleton._instances.pop(ConfigManager, None)
    ConfigManager(config_path=config_path).load_config()
    # A new config is a new identity mnemonic, and the node's TLS certificate is cached
    # for the process: left alone, a test that serves and dials itself would present the
    # certificate of an earlier config's identity and fail its own pinning.
    try:
        from src.identity.tls_identity import certificate_and_key
        certificate_and_key.cache_clear()
    except ImportError:
        pass
