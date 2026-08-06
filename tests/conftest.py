"""Test bootstrap.

Most test modules construct a ``ConfigManager`` at import time, which raises
``FileNotFoundError`` when ``config.yaml`` is absent. The modules catch that and
``skipIf`` themselves, so a bare checkout runs green while skipping the bulk of
the suite (86 of 104 tests). pytest imports ``conftest.py`` before collecting
those modules, so materialising a config here — from the shipped
``config.example.yaml`` — lets the real tests actually run. Any file we create
is removed on exit so a developer's working tree is left untouched.
"""
import atexit
import os
import shutil

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG = os.path.join(_ROOT, "config.yaml")
_EXAMPLE = os.path.join(_ROOT, "config.example.yaml")

if not os.path.exists(_CONFIG) and os.path.exists(_EXAMPLE):
    shutil.copyfile(_EXAMPLE, _CONFIG)

    @atexit.register
    def _remove_generated_config():
        try:
            os.remove(_CONFIG)
        except OSError:
            pass
