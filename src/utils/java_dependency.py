import importlib
import os
import sys
from typing import Optional

from src.utils.config import ConfigManager


class JavaDependencyMissing(RuntimeError):
    """Raised when a Java-backed Ergo/Reputation feature is used without Java."""


_SLF4J_STARTUP_MESSAGES = {
    "SLF4J: Failed to load class \"org.slf4j.impl.StaticLoggerBinder\".",
    "SLF4J: Defaulting to no-operation (NOP) logger implementation",
    "SLF4J: See http://www.slf4j.org/codes.html#StaticLoggerBinder for further details.",
}


class _Slf4jStderrFilter:
    """Forward Java stderr except for SLF4J's harmless missing-binder notice."""

    def __init__(self, target):
        self._target = target
        self._buffer = ""

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        self._buffer += str(data)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.rstrip("\r") not in _SLF4J_STARTUP_MESSAGES:
                self._target.write(line + "\n")

    def flush(self):
        if self._buffer:
            if self._buffer.rstrip("\r") not in _SLF4J_STARTUP_MESSAGES:
                self._target.write(self._buffer)
            self._buffer = ""
        self._target.flush()


_java_stderr_filter_installed = False


def _hide_slf4j_startup_notice() -> None:
    """Hide only SLF4J's missing optional binding message from the embedded JVM."""
    global _java_stderr_filter_installed
    if _java_stderr_filter_installed:
        return

    jpype = importlib.import_module("jpype")
    if not jpype.isJVMStarted():
        return
    redirect_stderr = getattr(jpype, "redirectStdErr", None)
    if not callable(redirect_stderr):
        return
    try:
        redirect_stderr(_Slf4jStderrFilter(sys.stderr))
    except Exception:
        # This is cosmetic only: a JVM that cannot redirect stderr must remain
        # usable for payments and reputation operations.
        return
    _java_stderr_filter_installed = True


def _detect_main_dir() -> str:
    try:
        main_dir = ConfigManager().get("MAIN_DIR")
        if main_dir:
            return str(main_dir)
    except Exception:
        pass
    return os.getcwd()


def get_java_install_command() -> str:
    main_dir = _detect_main_dir()
    return f"sudo /bin/bash {main_dir}/bash/install_java.sh {main_dir}"


def build_java_dependency_message(feature: Optional[str] = None) -> str:
    feature_text = f" to use {feature}" if feature else ""
    return (
        f"Java is not installed {feature_text}. "
        f"Install it with `{get_java_install_command()}`."
    )


def log_java_dependency_warning(logger_fn, feature: Optional[str] = None) -> str:
    message = build_java_dependency_message(feature=feature)
    logger_fn(message)
    return message


def raise_java_dependency_missing(feature: Optional[str] = None) -> None:
    raise JavaDependencyMissing(build_java_dependency_message(feature=feature))


def ensure_java_runtime(feature: Optional[str] = None) -> None:
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        if os.path.exists(os.path.join(java_home, "bin", "java")):
            return
        raise_java_dependency_missing(feature=feature)

    try:
        configured_java_home = ConfigManager().get("dependencies.java.JAVA_HOME")
    except Exception:
        configured_java_home = None

    if configured_java_home and os.path.exists(os.path.join(str(configured_java_home), "bin", "java")):
        return

    raise_java_dependency_missing(feature=feature)


def require_java_module(module_name: str, *, feature: Optional[str] = None):
    ensure_java_runtime(feature=feature)
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise JavaDependencyMissing(build_java_dependency_message(feature=feature)) from exc


def ensure_ergpy_jvm(feature: Optional[str] = None) -> None:
    helper_functions = require_java_module("ergpy.helper_functions", feature=feature)

    @helper_functions.initialize_jvm
    def _noop():
        return None

    _noop()
    _hide_slf4j_startup_notice()
