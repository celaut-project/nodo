import os
import shutil
import tempfile

import yaml


_CONFIG_DIRS = []


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
    config.setdefault("main", {})["MAIN_DIR"] = config_dir.name
    config["main"]["STORAGE"] = os.path.join(config_dir.name, "storage")
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, indent=2)

    Singleton._instances.pop(ConfigManager, None)
    ConfigManager(config_path=config_path).load_config()
