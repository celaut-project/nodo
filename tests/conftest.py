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


import pytest  # noqa: E402


# Scripts that drive a running node (`tests/main.py` reads its services file and dials
# its gateway). They are not tests of this code, so a bare `pytest tests` must not try
# to import them; run them by hand against a node.
collect_ignore = ["test_build.py", "test_start_service.py"]


@pytest.fixture(autouse=True)
def _fresh_query_cache():
    """The query cache (#456) is process-wide; a test must not inherit another's answers."""
    from src.utils.singleton import Singleton
    from src.utils.tools.query_cache import QueryCache
    Singleton._instances.pop(QueryCache, None)
    yield
    Singleton._instances.pop(QueryCache, None)
