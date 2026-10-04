"""The pack registry (``src/utils/pack_registry.py``) and ``nodo pack/packs/pack_cancel``.

A real pack needs an x86_64+KVM node (or a packer service), so the processes here
are fakes: a small script that registers through :func:`pack_registry.run` exactly
the way ``nodo pack`` does, and then succeeds, fails, hangs or dies on cue. What is
under test is everything around the packer -- the record, the detach handshake,
progress, the outcome, cancellation, stale records and pruning.
"""

import io
import json
import os
import signal
import sys
import textwrap
import threading
import time
from contextlib import redirect_stdout

import pytest

from src.utils import pack_registry as registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_ID = "ab" * 32

FAKE_PACK = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, {root!r})
    from src.utils import pack_registry as r

    mode = os.environ.get("FAKE_PACK_MODE", "ok")
    if mode == "early":
        print("Error: config.yaml is unreadable", flush=True)
        sys.exit(1)

    def pack():
        print("Cloning into 'demo'...", flush=True)
        r.stage("building")
        if mode == "ok":
            time.sleep(0.3)
            print("Service ID -> {sid}", flush=True)
            return "{sid}"
        if mode == "fail":
            r.note_error("Error in the compilation process: COPY failed")
            r.note_error("Packing produced no service id")
            return None
        if mode == "hang":
            try:
                time.sleep(120)
            finally:
                print("cleanup ran", flush=True)
        if mode == "stubborn":
            import signal
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(120)
        if mode == "crash":
            os._exit(9)

    r.run(sys.argv[-1], "dir", "local", pack)
    """
)


@pytest.fixture
def packs_dir(tmp_path, monkeypatch):
    directory = tmp_path / "packs"
    monkeypatch.setenv(registry.DIR_ENV, str(directory))
    monkeypatch.delenv(registry.ID_ENV, raising=False)
    return str(directory)


@pytest.fixture
def this_process_packs(monkeypatch):
    """A record with this process's pid reads as running. ``pid_alive`` looks for
    "pack" in the arguments of the process, and pytest does not have it."""
    real = registry.pid_alive
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == os.getpid() or real(pid))


@pytest.fixture
def fake_pack(tmp_path):
    script = tmp_path / "fake_pack.py"
    script.write_text(FAKE_PACK.format(root=ROOT, sid=SERVICE_ID))
    # "pack" in argv, as in `nodo.py pack <source>`: pid_alive looks for it in /proc.
    return [sys.executable, str(script), "pack"]


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _status(pack_id, directory):
    found = registry.find(pack_id, directory)
    return found[0] if found else None


# -- validate_source ----------------------------------------------------------------------


class TestValidateSource:
    def test_https_git_url(self):
        assert registry.validate_source("https://github.com/celaut-project/nodo.git") == (
            "git", "https://github.com/celaut-project/nodo.git")

    def test_https_url_without_dot_git_and_with_subdir(self):
        url = "https://github.com/celaut-basics/demo-service#services/hello"
        assert registry.validate_source(url) == ("git", url)

    @pytest.mark.parametrize("url", [
        "http://github.com/a/b.git",
        "ssh://git@github.com/a/b.git",
        "git@github.com:a/b.git",
        "git://github.com/a/b.git",
    ])
    def test_refuses_non_https_remotes(self, url):
        with pytest.raises(ValueError):
            registry.validate_source(url)

    def test_refuses_a_url_without_a_repository(self):
        with pytest.raises(ValueError, match="not a repository URL"):
            registry.validate_source("https://github.com/")

    def test_refuses_a_subdir_that_climbs_out(self):
        with pytest.raises(ValueError, match="subdirectory"):
            registry.validate_source("https://github.com/a/b.git#../../etc")

    def test_relative_directory_resolves_against_base(self, tmp_path):
        (tmp_path / "proj").mkdir()
        assert registry.validate_source("proj", base_dir=str(tmp_path)) == (
            "dir", str(tmp_path / "proj"))

    def test_missing_directory(self, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            registry.validate_source(str(tmp_path / "nope"))

    def test_a_file_is_not_a_project(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        with pytest.raises(ValueError, match="is a file"):
            registry.validate_source(str(tmp_path / "Dockerfile"))

    def test_a_directory_named_like_http_is_a_directory(self, tmp_path):
        # The old check was `"http" in path[:4]`, which took ./httpd for a URL.
        (tmp_path / "httpd").mkdir()
        assert registry.validate_source("httpd", base_dir=str(tmp_path))[0] == "dir"

    def test_empty(self):
        with pytest.raises(ValueError):
            registry.validate_source("  ")


# -- run(): the record a pack keeps ------------------------------------------------------


class TestRun:
    def test_success_records_service_id(self, packs_dir):
        seen = {}

        def pack():
            registry.stage("building")
            seen.update(registry._read(registry.record_path(
                registry._current["record"]["id"], packs_dir)))
            return SERVICE_ID

        service_id, pack_id = registry.run("/src/demo", "dir", "local", pack, directory=packs_dir)
        assert service_id == SERVICE_ID
        assert seen["status"] == "running" and seen["stage"] == "building"
        record = registry._read(registry.record_path(pack_id, packs_dir))
        assert record["status"] == "done"
        assert record["service_id"] == SERVICE_ID
        assert record["finished_at"] and record["stage"] is None
        assert record["detached"] is False and record["log"] is None

    def test_failure_keeps_the_first_reason(self, packs_dir):
        def pack():
            registry.note_error("Error in the compilation\n process: COPY failed")
            registry.note_error("Packing produced no service id")
            return None

        service_id, pack_id = registry.run("/src/demo", "dir", "local", pack, directory=packs_dir)
        assert service_id is None
        record = registry._read(registry.record_path(pack_id, packs_dir))
        assert record["status"] == "failed"
        assert record["error"] == "Error in the compilation process: COPY failed"

    def test_use_packer_removes_the_error_of_the_previous_packer(self, packs_dir):
        def pack():
            registry.note_error("Could not reach the packer service")
            registry.use_packer("local")
            registry.note_error("Error in the compilation process: COPY failed")
            return None

        _, pack_id = registry.run("/src/demo", "dir", "service", pack, directory=packs_dir)
        record = registry._read(registry.record_path(pack_id, packs_dir))
        assert record["packer"] == "local"
        assert record["error"] == "Error in the compilation process: COPY failed"

    def test_failure_without_a_reason_points_at_the_log(self, packs_dir):
        _, pack_id = registry.run("/src/demo", "dir", "local", lambda: None, directory=packs_dir)
        assert "no service id" in registry._read(registry.record_path(pack_id, packs_dir))["error"]

    def test_an_exception_is_recorded_and_raised(self, packs_dir):
        def pack():
            raise RuntimeError("bug")

        with pytest.raises(RuntimeError):
            registry.run("/src/demo", "dir", "local", pack, directory=packs_dir)
        (record,) = registry.list_packs(packs_dir)
        assert record["status"] == "failed" and record["error"] == "bug"

    def test_sigterm_cancels_through_finally_blocks(self, packs_dir):
        cleaned = []

        def pack():
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
            except Exception:  # what the packers catch must not swallow a cancel
                return "wrong"
            finally:
                cleaned.append(True)

        service_id, pack_id = registry.run("/src/demo", "dir", "local", pack, directory=packs_dir)
        assert service_id is None and cleaned == [True]
        assert registry._read(registry.record_path(pack_id, packs_dir))["status"] == "cancelled"
        assert signal.getsignal(signal.SIGTERM) is not registry._raise_cancelled

    def test_stage_and_note_error_are_noops_outside_a_pack(self, packs_dir):
        registry.stage("building")
        registry.note_error("nothing to attach it to")
        assert not os.path.exists(packs_dir)

    def test_queued_flips_status(self, packs_dir):
        states = []

        def pack():
            registry.queued(True)
            states.append(registry._current["record"]["status"])
            registry.queued(False)
            states.append(registry._current["record"]["status"])
            return SERVICE_ID

        registry.run("/src/demo", "dir", "local", pack, directory=packs_dir)
        assert states == ["queued", "running"]

    def test_unwritable_registry_does_not_stop_the_pack(self, tmp_path, capsys):
        blocker = tmp_path / "file"
        blocker.write_text("")
        service_id, _ = registry.run("/src", "dir", "local", lambda: SERVICE_ID,
                                     directory=str(blocker / "packs"))
        assert service_id == SERVICE_ID
        assert "not listed" in capsys.readouterr().out


# -- Reading -----------------------------------------------------------------------------


def _record(directory, pack_id, status, started, pid=None, **extra):
    record = registry.new_record(pack_id, "/src/" + pack_id, "dir", "local", detached=False,
                                 pid=pid if pid is not None else os.getpid())
    record.update(status=status, started_at=started, **extra)
    registry.write(record, directory)
    return record


class TestListing:
    def test_newest_first_with_ages(self, packs_dir):
        now = int(time.time())
        _record(packs_dir, "aaaa0001", "done", now - 100, finished_at=now - 40,
                service_id=SERVICE_ID)
        _record(packs_dir, "aaaa0002", "running", now - 10)
        packs = registry.list_packs(packs_dir)
        assert [p["id"] for p in packs] == ["aaaa0002", "aaaa0001"]
        assert packs[1]["duration_secs"] == 60
        assert packs[0]["age_secs"] >= 10

    def test_dead_running_pack_is_marked_failed(self, packs_dir):
        _record(packs_dir, "dead0001", "running", int(time.time()), pid=2 ** 22 + 12345)
        (record,) = registry.list_packs(packs_dir)
        assert record["status"] == "failed" and record["error"] == registry.LOST_ERROR
        on_disk = registry._read(registry.record_path("dead0001", packs_dir))
        assert on_disk["status"] == "failed"

    def test_read_only_listing_does_not_rewrite(self, packs_dir):
        _record(packs_dir, "dead0002", "queued", int(time.time()), pid=2 ** 22 + 12346)
        assert registry.list_packs(packs_dir, sweep=False)[0]["status"] == "failed"
        assert registry._read(registry.record_path("dead0002", packs_dir))["status"] == "queued"

    def test_prunes_old_finished_packs_but_never_running_ones(self, packs_dir, this_process_packs):
        now = int(time.time())
        for index in range(registry.KEEP_FINISHED + 5):
            _record(packs_dir, f"old{index:05d}", "done", now - 1000 + index)
            with open(registry.log_path(f"old{index:05d}", packs_dir), "w") as handle:
                handle.write("x\n")
        _record(packs_dir, "live0001", "running", now - 5000)
        packs = registry.list_packs(packs_dir)
        assert len(packs) == registry.KEEP_FINISHED + 1
        assert "live0001" in [p["id"] for p in packs]
        assert not os.path.exists(registry.record_path("old00000", packs_dir))
        assert not os.path.exists(registry.log_path("old00000", packs_dir))

    def test_find_exact_and_prefix(self, packs_dir):
        now = int(time.time())
        _record(packs_dir, "abcd0001", "done", now)
        _record(packs_dir, "abcd0002", "done", now)
        assert len(registry.find("abcd", packs_dir)) == 2
        assert [p["id"] for p in registry.find("abcd0002", packs_dir)] == ["abcd0002"]
        assert registry.find("", packs_dir) == []

    def test_log_tail_keeps_the_last_state_of_a_redrawn_line(self, packs_dir, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("one\nProcessing |\rProcessing /\rProcessing -\n\n")
        record = {"log": str(log)}
        assert registry.log_tail(record) == ["one", "Processing -", ""]
        assert registry.last_line(record) == "Processing -"

    def test_ignores_garbage(self, packs_dir):
        os.makedirs(packs_dir)
        with open(os.path.join(packs_dir, "junk.json"), "w") as handle:
            handle.write("{not json")
        assert registry.list_packs(packs_dir) == []


# -- Detach and cancel, with a fake pack process -------------------------------------------


class TestDetached:
    def test_handshake_then_done(self, packs_dir, fake_pack):
        record, error = registry.spawn_detached("/src/demo", "unused", directory=packs_dir,
                                                command=fake_pack, timeout_s=20)
        assert error is None
        assert record["detached"] is True and record["log"].endswith(record["id"] + ".log")
        assert record["status"] in ("running", "done")
        done = _wait_for(lambda: (_status(record["id"], packs_dir) or {}).get("status") == "done"
                         and _status(record["id"], packs_dir))
        assert done["service_id"] == SERVICE_ID
        assert done["last_line"] == f"Service ID -> {SERVICE_ID}"
        assert "Cloning into 'demo'..." in registry.log_tail(done)

    def test_failure_reason(self, packs_dir, fake_pack, monkeypatch):
        monkeypatch.setenv("FAKE_PACK_MODE", "fail")
        record, _ = registry.spawn_detached("/src/demo", "unused", directory=packs_dir,
                                            command=fake_pack, timeout_s=20)
        failed = _wait_for(lambda: (_status(record["id"], packs_dir) or {}).get("status") == "failed"
                           and _status(record["id"], packs_dir))
        assert failed["error"] == "Error in the compilation process: COPY failed"

    def test_exit_before_registering_reports_last_line(self, packs_dir, fake_pack, monkeypatch):
        monkeypatch.setenv("FAKE_PACK_MODE", "early")
        record, error = registry.spawn_detached("/src/demo", "unused", directory=packs_dir,
                                                command=fake_pack, timeout_s=20)
        assert record is None
        assert error == "Error: config.yaml is unreadable"
        assert os.listdir(packs_dir) == []

    def test_crash_mid_pack_reads_as_failed(self, packs_dir, fake_pack, monkeypatch):
        monkeypatch.setenv("FAKE_PACK_MODE", "crash")
        record, _ = registry.spawn_detached("/src/demo", "unused", directory=packs_dir,
                                            command=fake_pack, timeout_s=20)
        failed = _wait_for(lambda: (_status(record["id"], packs_dir) or {}).get("status") == "failed"
                           and _status(record["id"], packs_dir))
        assert failed["error"] == registry.LOST_ERROR

    def test_cancel_unwinds_cleanly(self, packs_dir, fake_pack, monkeypatch):
        monkeypatch.setenv("FAKE_PACK_MODE", "hang")
        record, _ = registry.spawn_detached("/src/demo", "unused", directory=packs_dir,
                                            command=fake_pack, timeout_s=20)
        _wait_for(lambda: (_status(record["id"], packs_dir) or {}).get("stage") == "building")
        ok, message = registry.cancel(_status(record["id"], packs_dir), packs_dir, grace_s=10)
        assert ok and message == f"Pack {record['id']} cancelled."
        final = _status(record["id"], packs_dir)
        assert final["status"] == "cancelled" and final["error"] == "cancelled"
        assert "cleanup ran" in registry.log_tail(final)
        assert not registry.pid_alive(record["pid"])

    def test_cancel_kills_one_that_ignores_sigterm(self, packs_dir, fake_pack, monkeypatch):
        monkeypatch.setenv("FAKE_PACK_MODE", "stubborn")
        record, _ = registry.spawn_detached("/src/demo", "unused", directory=packs_dir,
                                            command=fake_pack, timeout_s=20)
        _wait_for(lambda: (_status(record["id"], packs_dir) or {}).get("stage") == "building")
        time.sleep(0.3)  # let it install SIG_IGN
        ok, message = registry.cancel(_status(record["id"], packs_dir), packs_dir, grace_s=0.5)
        assert ok and "killed" in message
        _wait_for(lambda: not registry.pid_alive(record["pid"]))
        assert _status(record["id"], packs_dir)["status"] == "cancelled"

    def test_cancel_refuses_a_finished_pack(self, packs_dir):
        record = _record(packs_dir, "done0001", "done", int(time.time()))
        ok, message = registry.cancel(record, packs_dir)
        assert not ok and "not running (done)" in message


# -- The commands -----------------------------------------------------------------------


def _json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestCommands:
    def test_pack_rejects_bad_source_as_json(self, packs_dir, capsys):
        from src.commands.packs import pack_command

        assert pack_command(["http://example.com/a.git", "--detach", "--json"]) == 1
        assert "https" in _json(capsys)["error"]

    def test_pack_usage(self, packs_dir, capsys):
        from src.commands.packs import pack_command

        assert pack_command([]) == 1
        assert pack_command(["a", "b"]) == 1
        assert "Usage: nodo pack" in capsys.readouterr().out

    def test_pack_detach_json(self, packs_dir, fake_pack, tmp_path, capsys, monkeypatch):
        from src.commands import packs

        (tmp_path / "proj").mkdir()
        monkeypatch.setenv("ORIGINAL_DIR", str(tmp_path))
        real_detach = packs.detach
        monkeypatch.setattr(packs, "detach",
                            lambda source, as_json=False, local=False:
                            real_detach(source, as_json, command=fake_pack, local=local))
        assert packs.pack_command(["proj", "--detach", "--json"]) == 0
        document = _json(capsys)
        assert document["pack"]["source"] == str(tmp_path / "proj")
        assert document["pack"]["detached"] is True

    def test_pack_local_goes_to_the_packer_and_the_record(self, packs_dir, tmp_path, monkeypatch):
        import types
        from src.commands import packs

        (tmp_path / "proj").mkdir()
        monkeypatch.setenv("ORIGINAL_DIR", str(tmp_path))
        calls = []

        def pack(directory, local=False):
            calls.append((directory, local))
            return SERVICE_ID

        monkeypatch.setitem(sys.modules, "src.commands.packer.zip_with_dockerfile.pack",
                            types.SimpleNamespace(pack=pack))
        with redirect_stdout(io.StringIO()):
            assert packs.pack_command(["proj", "--local"]) == 0
        assert calls == [(str(tmp_path / "proj"), True)]
        [record] = registry.list_packs(packs_dir)
        assert record["packer"] == "local"

    def test_pack_local_detach_gives_the_flag_to_the_child(self, packs_dir, tmp_path, monkeypatch):
        from src.commands import packs

        (tmp_path / "proj").mkdir()
        monkeypatch.setenv("ORIGINAL_DIR", str(tmp_path))
        seen = {}

        def spawn_detached(source, nodo_py, **kwargs):
            seen.update(kwargs, source=source)
            return {"id": "3f9a0c12", "pid": 1, "source": source, "log": "x.log"}, None

        monkeypatch.setattr(registry, "spawn_detached", spawn_detached)
        with redirect_stdout(io.StringIO()):
            assert packs.pack_command(["proj", "--detach", "--local"]) == 0
        assert seen["source"] == str(tmp_path / "proj")
        assert seen["options"] == ["--local"]

    def test_packs_list_and_inspect(self, packs_dir, capsys, this_process_packs):
        from src.commands.packs import list_packs

        now = int(time.time())
        _record(packs_dir, "list0001", "done", now - 30, finished_at=now, service_id=SERVICE_ID)
        _record(packs_dir, "list0002", "running", now, stage="building")
        assert list_packs(as_json=True)
        assert [p["id"] for p in _json(capsys)["packs"]] == ["list0002", "list0001"]
        assert list_packs(as_json=True, active_only=True)
        assert [p["id"] for p in _json(capsys)["packs"]] == ["list0002"]
        assert list_packs("list0001", as_json=True)
        assert _json(capsys)["pack"]["log_tail"] == []
        assert list_packs()
        table = capsys.readouterr().out
        assert "building" in table and SERVICE_ID in table
        assert not list_packs("nope", as_json=True)
        assert "No pack 'nope'" in _json(capsys)["error"]

    def test_render_one(self):
        from src.commands.packs import render_one

        text = render_one({"id": "x1", "status": "failed", "source": "/p", "kind": "dir",
                           "packer": "local", "pid": 1, "error": "boom",
                           "started_at": 0, "finished_at": 5, "duration_secs": 5},
                          ["a", "b"])
        assert "error       boom" in text and "Last log lines:" in text

    def test_render_empty_list(self):
        from src.commands.packs import render_list

        assert "No packs on record" in render_list([])

    def test_pack_cancel_json(self, packs_dir, capsys):
        from src.commands.packs import cancel_packs

        _record(packs_dir, "fin00001", "failed", int(time.time()), error="x")
        assert not cancel_packs(["fin00001"], as_json=True)
        document = _json(capsys)
        assert document["failed"] == ["fin00001"] and "not running" in document["error"]
        assert not cancel_packs([], as_json=True)
        assert "Usage" in _json(capsys)["error"]


# -- The local packer queues a detached pack behind another ---------------------------------


def test_detached_local_pack_waits_for_the_lock(packs_dir, tmp_path, monkeypatch):
    try:
        from src.commands.packer.zip_with_dockerfile import local_pack
    except Exception as e:  # pragma: no cover - needs the packer import graph
        pytest.skip(f"local packer not importable here: {e}")
    import fcntl

    monkeypatch.setattr(local_pack, "_pack_lock_path", lambda: str(tmp_path / "pack.lock"))
    holder = open(tmp_path / "pack.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    assert local_pack._acquire_pack_lock(wait=False) is None

    got, states = [], []

    def pack():
        thread = threading.Thread(target=lambda: got.append(local_pack._acquire_pack_lock(wait=True)))
        thread.start()
        _wait_for(lambda: registry._current["record"]["status"] == "queued")
        states.append("queued")
        fcntl.flock(holder, fcntl.LOCK_UN)
        thread.join(5)
        states.append(registry._current["record"]["status"])
        return SERVICE_ID

    with redirect_stdout(io.StringIO()):
        registry.run("/src", "dir", "local", pack, directory=packs_dir)
    assert states == ["queued", "running"] and got and got[0] is not None
    local_pack._release_pack_lock(got[0])
    holder.close()
