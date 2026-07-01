"""Unit tests for the low-demand fallback scheduler scaffold.

These tests mock the resource readings and the launch path so nothing real is
started and no network is touched. They cover the config-threshold reading and
the ``should_run_fallback`` decision logic.
"""

import importlib
import sys

import pytest

low_demand = importlib.import_module("src.core_services.low_demand")

# Real implementation, captured before the autouse fixture stubs it, so the polled
# detection tests can exercise the genuine _real_workload_present logic.
_REAL_WORKLOAD_PRESENT = low_demand._real_workload_present


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
    # Node RAM pool has headroom by default (system-wide + reservation cross-check).
    monkeypatch.setattr(low_demand, "_iobigdata_has_headroom", lambda: True)
    # Fresh scheduler state for every test (module singleton).
    low_demand._state.consecutive_below = 0
    low_demand._state.last_tick_monotonic = None
    low_demand._state.running_token = None
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


def test_consecutive_polls_clamped_to_minimum(monkeypatch):
    monkeypatch.setattr(low_demand, "_get_float", lambda k, d: 0.0)
    assert low_demand.consecutive_polls() >= 1


# --- RAM reservation cross-check (Josemi decision #1) -------------------------


def test_resources_below_threshold_blocks_when_pool_exhausted(monkeypatch):
    # System-wide CPU/RAM look fine, but the node's own pool has no headroom.
    monkeypatch.setattr(
        low_demand,
        "_get_float",
        lambda k, d: {"CPU_MAX_PERCENT": 40.0, "MEM_MAX_PERCENT": 60.0}[k],
    )
    monkeypatch.setattr(low_demand, "_current_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(low_demand, "_current_mem_percent", lambda: 20.0)
    monkeypatch.setattr(low_demand, "_iobigdata_has_headroom", lambda: False)
    assert low_demand.resources_below_threshold() is False


# --- polled real-workload detection (Josemi decision #3) ---------------------


def _install_fake_sql_connection(monkeypatch, ids):
    """Inject a fake ``src.database.sql_connection`` so we don't import the heavy real
    module (it pulls in grpc/bee_rpc). ``_real_workload_present`` imports it lazily."""
    import types

    fake = types.ModuleType("src.database.sql_connection")

    class _FakeSC:
        def get_all_internal_containers_ids(self):
            return list(ids)

    fake.SQLConnection = _FakeSC
    monkeypatch.setitem(sys.modules, "src.database.sql_connection", fake)


def test_real_workload_excludes_own_fallback_instance(monkeypatch):
    monkeypatch.setattr(low_demand, "_real_workload_present", _REAL_WORKLOAD_PRESENT)
    _install_fake_sql_connection(monkeypatch, ["fallback-tok"])
    low_demand._state.running_token = "fallback-tok"
    # Only the fallback's own instance is running -> NOT a real workload.
    assert low_demand._real_workload_present() is False


def test_real_workload_detected_when_other_instance_present(monkeypatch):
    monkeypatch.setattr(low_demand, "_real_workload_present", _REAL_WORKLOAD_PRESENT)
    _install_fake_sql_connection(monkeypatch, ["fallback-tok", "real-instance-1"])
    low_demand._state.running_token = "fallback-tok"
    # A different (real) instance is running alongside the fallback -> busy.
    assert low_demand._real_workload_present() is True


# --- preemption: ALWAYS STOP, never pause (Josemi decision #2) ---------------


def test_stop_running_fallback_calls_stop_instance(monkeypatch):
    import types

    calls = {}

    def _fake_stop(token):
        calls["token"] = token
        return 0

    # Inject a fake src.manager.manager so we don't import the heavy real module and
    # can assert the STOP path (there is no pause primitive to call).
    fake_mgr = types.ModuleType("src.manager.manager")
    fake_mgr.stop_instance = _fake_stop
    monkeypatch.setitem(sys.modules, "src.manager.manager", fake_mgr)

    low_demand._state.running_token = "tok-123"
    low_demand._stop_running_fallback()

    assert calls == {"token": "tok-123"}
    # Token cleared after stopping so we don't try to stop it again.
    assert low_demand._state.running_token is None


# --- scheduler_tick: hysteresis + preemption ---------------------------------


def test_scheduler_tick_starts_only_after_hysteresis(monkeypatch):
    # Idle + under threshold every tick; cadence gate disabled (poll interval 0).
    monkeypatch.setattr(low_demand, "poll_interval_seconds", lambda: 0)
    monkeypatch.setattr(low_demand, "consecutive_polls", lambda: 3)
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    monkeypatch.setattr(low_demand, "_real_workload_present", lambda: False)

    launches = {"n": 0}
    monkeypatch.setattr(low_demand, "run_fallback_once", lambda: launches.__setitem__("n", launches["n"] + 1))

    # First two ticks accumulate hysteresis but do NOT start.
    low_demand.scheduler_tick()
    assert launches["n"] == 0
    assert low_demand._state.consecutive_below == 1
    low_demand.scheduler_tick()
    assert launches["n"] == 0
    assert low_demand._state.consecutive_below == 2

    # Third consecutive clean poll crosses the hysteresis threshold -> start.
    low_demand.scheduler_tick()
    assert launches["n"] == 1
    assert low_demand._state.consecutive_below == 3


def test_scheduler_tick_no_start_when_over_threshold(monkeypatch):
    monkeypatch.setattr(low_demand, "poll_interval_seconds", lambda: 0)
    monkeypatch.setattr(low_demand, "consecutive_polls", lambda: 1)
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: False)
    monkeypatch.setattr(low_demand, "_real_workload_present", lambda: False)

    def _must_not_launch():  # pragma: no cover
        raise AssertionError("must not start the fallback when over threshold")

    monkeypatch.setattr(low_demand, "run_fallback_once", _must_not_launch)

    stops = {"n": 0}
    monkeypatch.setattr(low_demand, "_stop_running_fallback", lambda: stops.__setitem__("n", stops["n"] + 1))

    # Pretend some hysteresis had accumulated; over-threshold must reset it.
    low_demand._state.consecutive_below = 5
    low_demand.scheduler_tick()

    assert stops["n"] == 1
    assert low_demand._state.consecutive_below == 0


def test_scheduler_tick_real_request_preempts_and_stops(monkeypatch):
    # Under threshold but a real request is present -> STOP (never start/pause).
    monkeypatch.setattr(low_demand, "poll_interval_seconds", lambda: 0)
    monkeypatch.setattr(low_demand, "consecutive_polls", lambda: 1)
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    monkeypatch.setattr(low_demand, "_real_workload_present", lambda: True)

    def _must_not_launch():  # pragma: no cover
        raise AssertionError("must not start the fallback while a real request is present")

    monkeypatch.setattr(low_demand, "run_fallback_once", _must_not_launch)

    stops = {"n": 0}
    monkeypatch.setattr(low_demand, "_stop_running_fallback", lambda: stops.__setitem__("n", stops["n"] + 1))

    low_demand._state.consecutive_below = 2
    low_demand.scheduler_tick()

    assert stops["n"] == 1
    assert low_demand._state.consecutive_below == 0


def test_scheduler_tick_respects_poll_interval_cadence(monkeypatch):
    # A large poll interval means a second immediate tick is a no-op.
    monkeypatch.setattr(low_demand, "poll_interval_seconds", lambda: 3600)
    monkeypatch.setattr(low_demand, "consecutive_polls", lambda: 1)
    monkeypatch.setattr(low_demand, "resources_below_threshold", lambda: True)
    monkeypatch.setattr(low_demand, "_real_workload_present", lambda: False)

    launches = {"n": 0}
    monkeypatch.setattr(low_demand, "run_fallback_once", lambda: launches.__setitem__("n", launches["n"] + 1))

    low_demand.scheduler_tick()  # runs (first ever tick)
    low_demand.scheduler_tick()  # gated by cadence -> no-op
    assert launches["n"] == 1


def test_scheduler_tick_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(low_demand, "is_enabled", lambda: False)

    def _boom():  # pragma: no cover
        raise AssertionError("disabled tick must do nothing")

    monkeypatch.setattr(low_demand, "resources_below_threshold", _boom)
    monkeypatch.setattr(low_demand, "run_fallback_once", _boom)
    monkeypatch.setattr(low_demand, "_stop_running_fallback", _boom)

    low_demand.scheduler_tick()  # returns immediately, nothing invoked


def test_scheduler_tick_never_raises(monkeypatch):
    monkeypatch.setattr(low_demand, "poll_interval_seconds", lambda: 0)

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(low_demand, "resources_below_threshold", _boom)
    # Must swallow and return None, never propagate into the manager loop.
    assert low_demand.scheduler_tick() is None
