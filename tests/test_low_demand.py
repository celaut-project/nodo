"""Unit tests for the low-demand fallback scheduler scaffold.

These tests mock the resource readings and the launch path so nothing real is
started and no network is touched. They cover the config-threshold reading and
the ``should_run_fallback`` decision logic.
"""

import importlib
import sys

import pytest

low_demand = importlib.import_module("src.core_services.low_demand")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Default happy-path stubs; individual tests override as needed."""
    # Enabled + a configured id by default.
    monkeypatch.setattr(low_demand, "is_enabled", lambda: True)
    monkeypatch.setattr(
        low_demand, "get_core_service_id", lambda name: "deadbeef" if name else None
    )
    # No real workload by default.
    monkeypatch.setattr(low_demand, "_real_workload_present", lambda: False)
    yield


# --- threshold reading -------------------------------------------------------


def test_resources_below_threshold_true_when_under(monkeypatch):
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: {"CPU_MAX_PERCENT": 40.0, "MEM_MAX_PERCENT": 60.0}[k])
    monkeypatch.setattr(low_demand, "_current_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(low_demand, "_current_mem_percent", lambda: 20.0)
    assert low_demand.resources_below_threshold() is True


def test_resources_below_threshold_false_when_cpu_over(monkeypatch):
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: {"CPU_MAX_PERCENT": 40.0, "MEM_MAX_PERCENT": 60.0}[k])
    monkeypatch.setattr(low_demand, "_current_cpu_percent", lambda: 90.0)
    monkeypatch.setattr(low_demand, "_current_mem_percent", lambda: 20.0)
    assert low_demand.resources_below_threshold() is False


def test_resources_below_threshold_false_when_mem_over(monkeypatch):
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: {"CPU_MAX_PERCENT": 40.0, "MEM_MAX_PERCENT": 60.0}[k])
    monkeypatch.setattr(low_demand, "_current_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(low_demand, "_current_mem_percent", lambda: 99.0)
    assert low_demand.resources_below_threshold() is False


def test_resources_below_threshold_boundary_is_inclusive(monkeypatch):
    # Exactly at the threshold counts as "below" (<=).
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: {"CPU_MAX_PERCENT": 40.0, "MEM_MAX_PERCENT": 60.0}[k])
    monkeypatch.setattr(low_demand, "_current_cpu_percent", lambda: 40.0)
    monkeypatch.setattr(low_demand, "_current_mem_percent", lambda: 60.0)
    assert low_demand.resources_below_threshold() is True


def test_resources_below_threshold_fail_closed_when_reading_missing(monkeypatch):
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: 50.0)
    monkeypatch.setattr(low_demand, "_current_cpu_percent", lambda: None)
    monkeypatch.setattr(low_demand, "_current_mem_percent", lambda: 10.0)
    assert low_demand.resources_below_threshold() is False


# --- should_run_fallback -----------------------------------------------------


def test_should_run_true_when_idle_and_under_threshold(monkeypatch):
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    assert low_demand.should_run_fallback() is True


def test_should_run_false_when_disabled(monkeypatch):
    monkeypatch.setattr(low_demand, "is_enabled", lambda: False)
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    assert low_demand.should_run_fallback() is False


def test_should_run_false_when_no_id_configured(monkeypatch):
    monkeypatch.setattr(low_demand, "get_core_service_id", lambda name: None)
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    assert low_demand.should_run_fallback() is False


def test_should_run_false_when_over_threshold(monkeypatch):
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: False)
    assert low_demand.should_run_fallback() is False


def test_should_run_false_when_real_workload_present(monkeypatch):
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    monkeypatch.setattr(low_demand, "_real_workload_present", lambda: True)
    assert low_demand.should_run_fallback() is False


# --- run_fallback_once (launch path mocked) ----------------------------------


def test_run_fallback_once_launches_when_should_run(monkeypatch):
    monkeypatch.setattr(low_demand, "should_run_fallback", lambda: True)
    calls = {}

    def _fake_ensure(service_id, *, launch=True):
        calls["service_id"] = service_id
        calls["launch"] = launch
        return "http://1.2.3.4:5000"

    # ensure_core_service_running is imported inside the function, so patch the source module.
    runtime = importlib.import_module("src.core_services.runtime")
    monkeypatch.setattr(runtime, "ensure_core_service_running", _fake_ensure)

    endpoint = low_demand.run_fallback_once()
    assert endpoint == "http://1.2.3.4:5000"
    assert calls == {"service_id": "deadbeef", "launch": True}


def test_run_fallback_once_noop_when_should_not_run(monkeypatch):
    monkeypatch.setattr(low_demand, "should_run_fallback", lambda: False)

    runtime = importlib.import_module("src.core_services.runtime")

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("ensure_core_service_running must not be called")

    monkeypatch.setattr(runtime, "ensure_core_service_running", _boom)
    assert low_demand.run_fallback_once() is None


def test_run_fallback_once_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(low_demand, "should_run_fallback", _boom)
    # Must swallow and return None, never propagate.
    assert low_demand.run_fallback_once() is None


# --- misc helpers ------------------------------------------------------------


def test_poll_interval_clamped_to_minimum(monkeypatch):
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: 0.0)
    assert low_demand.poll_interval_seconds() >= 1
