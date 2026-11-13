"""The notice that the node writes to a service before it stops it (#645).

A service that declares ``notice_declaration`` has a virtio console. The node writes
``STOP <t>`` or ``HIBERNATE <t>`` to it, waits until ``<t>`` or until the VM ends, and then
stops or hibernates as before. A service with no declaration is stopped at once.
"""

import json
import os
import select
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2, pack_pb2
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import hibernation, maintain, stopping
    from src.packers.zip_with_dockerfile import ZipContainerPacker
    from src.utils import config_validation, dead_instances, notice
    from src.virtualizers import selection
    from src.virtualizers.ch import execute as ch
    from src.virtualizers.qemu import execute as qemu_exec
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]

skip = unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")


def _service(path=("dev", "hvc0"), system=("virtio-console",), fmt=("nodo-notice",)):
    service = celaut_pb2.Service()
    declaration = service.container.notice_declaration
    declaration.path.extend(path)
    declaration.system.tags.extend(system)
    declaration.format.tags.extend(fmt)
    return service


def _pack(declaration):
    """The ``pack.Service`` that the packer builds for a ``service.json`` key."""
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = {} if declaration is None else {"notice_declaration": declaration}
    packer.service = pack_pb2.Service()
    packer.parseNoticeDeclaration()
    return packer.service


@skip
class SpecTests(unittest.TestCase):
    def test_a_service_with_no_declaration_keeps_its_bytes(self):
        """Hash compatibility: the field is absent from the wire when it is not set."""
        service = celaut_pb2.Service()
        service.container.architecture.tags.append("linux/amd64")
        before = service.SerializeToString()
        self.assertFalse(service.container.HasField("notice_declaration"))
        reparsed = celaut_pb2.Service()
        reparsed.ParseFromString(before)
        self.assertEqual(reparsed.SerializeToString(), before)
        self.assertNotIn(b"hvc0", before)

    def test_a_declaration_is_part_of_the_spec(self):
        service = celaut_pb2.Service()
        before = service.SerializeToString()
        service.container.notice_declaration.CopyFrom(notice.default_declaration())
        self.assertNotEqual(service.SerializeToString(), before)
        self.assertEqual(list(service.container.notice_declaration.path), ["dev", "hvc0"])

    def test_the_pack_schema_carries_it_to_the_stored_schema(self):
        packed = _pack(None)
        stored = celaut_pb2.Service()
        stored.ParseFromString(packed.SerializeToString())
        self.assertEqual(
            list(stored.container.notice_declaration.system.tags), ["virtio-console"])


@skip
class PackerTests(unittest.TestCase):
    def test_no_key_writes_the_default(self):
        declaration = _pack(None).container.notice_declaration
        self.assertEqual(list(declaration.path), ["dev", "hvc0"])
        self.assertEqual(list(declaration.system.tags), ["virtio-console"])
        self.assertEqual(list(declaration.format.tags), ["nodo-notice"])

    def test_false_writes_no_declaration(self):
        self.assertFalse(_pack(False).container.HasField("notice_declaration"))

    def test_an_object_changes_what_it_gives_and_keeps_the_default_for_the_rest(self):
        declaration = _pack({"system": "virtio-vsock"}).container.notice_declaration
        self.assertEqual(list(declaration.system.tags), ["virtio-vsock"])
        self.assertEqual(list(declaration.path), ["dev", "hvc0"])
        self.assertEqual(list(declaration.format.tags), ["nodo-notice"])

    def test_a_path_is_split_in_segments(self):
        declaration = _pack({"path": "/dev/hvc1"}).container.notice_declaration
        self.assertEqual(list(declaration.path), ["dev", "hvc1"])

    def test_a_wrong_shape_is_refused(self):
        for bad in (True, "yes", 5, [], {"path": []}, {"system": ""}, {"format": [1]},
                    {"unknown": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _pack(bad)


@skip
class RefusalTests(unittest.TestCase):
    def test_the_one_combination_that_is_supported_is_not_refused(self):
        self.assertIsNone(notice.refusal(_service()))
        notice.enforce_supported(_service())

    def test_a_service_with_no_declaration_is_not_refused(self):
        self.assertIsNone(notice.refusal(celaut_pb2.Service()))
        notice.enforce_supported(celaut_pb2.Service())
        notice.enforce_supported(None)

    def test_an_unknown_path_system_or_format_is_refused(self):
        for service in (
            _service(path=("dev", "hvc1")),
            _service(system=("virtio-vsock",)),
            _service(fmt=("json",)),
            _service(system=()),
        ):
            with self.subTest(service=str(service)), self.assertRaises(
                    notice.UnsupportedNoticeException):
                notice.enforce_supported(service)

    def test_the_node_does_not_run_or_quote_a_service_that_it_refuses(self):
        """The same rule as for an architecture: the places that refuse one refuse this."""
        refused = _service(system=("virtio-vsock",))
        with self.assertRaises(notice.UnsupportedNoticeException):
            selection.select_virtualizer(service=refused)
        from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
        with self.assertRaises(notice.UnsupportedNoticeException):
            generate_estimated_cost(
                metadata=celaut_pb2.Metadata(), config=celaut_pb2.Configuration(),
                resources=celaut_pb2.Service.Container.Resources(), service=refused)

    def test_the_refusal_is_an_architecture_refusal_for_every_caller_that_handles_one(self):
        from src.virtualizers.architecture import UnsupportedArchitectureException
        self.assertTrue(issubclass(notice.UnsupportedNoticeException,
                                   UnsupportedArchitectureException))


@skip
class LineTests(unittest.TestCase):
    def test_the_format_of_a_line(self):
        self.assertEqual(notice.line(notice.STOP, 1700000000.9), b"STOP 1700000000\n")
        self.assertEqual(notice.line(notice.HIBERNATE, 5), b"HIBERNATE 5\n")

    def test_an_unknown_verb_is_refused(self):
        with self.assertRaises(ValueError):
            notice.line("KILL", 1)

    def test_a_line_is_short(self):
        self.assertLess(len(notice.line(notice.HIBERNATE, 2 ** 40)), notice.MAX_LINE_BYTES)

    def test_the_limit_is_now_plus_the_grace(self):
        self.assertEqual(notice.grace_deadline(15, now=100.0), 115.0)
        self.assertEqual(notice.grace_deadline(-3, now=100.0), 100.0)


@skip
class SocketTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "n.sock")

    def _serve(self, received):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            conn, _ = server.accept()
            with conn:
                received.append(conn.recv(256))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_the_node_writes_the_line_and_never_reads(self):
        received = []
        thread = self._serve(received)
        self.assertTrue(notice.send(self.path, notice.STOP, 1700000000))
        thread.join(timeout=5)
        self.assertEqual(received, [b"STOP 1700000000\n"])

    def test_nobody_listening_is_a_false_and_not_an_error(self):
        self.assertFalse(notice.send(self.path, notice.STOP, 1))

    def test_a_guest_that_writes_a_lot_does_not_block_the_node(self):
        """The node never reads from the socket: nothing the guest writes can stall it."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(1)
        self.addCleanup(server.close)
        self.assertTrue(notice.send(self.path, notice.STOP, 1, timeout=1.0))

    def test_the_socket_is_in_the_short_flat_directory(self):
        with mock.patch("src.virtualizers.microvm.paths.control_socket_dir",
                        return_value=Path("/run/nodo/ch")):
            path = notice.socket_path("a" * 64)
        self.assertEqual(path, "/run/nodo/ch/notice-" + "a" * 16 + ".sock")
        self.assertLess(len(path), 108)

    def test_a_vm_with_no_socket_has_no_device(self):
        with mock.patch.object(notice, "socket_path", return_value=self.path):
            self.assertFalse(notice.has_device("x"))
            Path(self.path).write_text("not a socket")
            self.assertFalse(notice.has_device("x"))
            os.unlink(self.path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(self.path)
            self.addCleanup(server.close)
            self.assertTrue(notice.has_device("x"))


@skip
class ConfigTests(unittest.TestCase):
    def _check(self, value):
        config_validation.validate_instances_config({"instances": {"STOP_GRACE_SECONDS": value}})

    def test_the_key_is_optional(self):
        config_validation.validate_instances_config({})
        config_validation.validate_instances_config({"instances": {}})

    def test_a_whole_number_of_zero_or_more_is_accepted(self):
        for value in (0, 1, 15, 3600):
            self._check(value)

    def test_anything_else_is_refused(self):
        for value in (-1, 1.5, "15", None, True, False, [15]):
            with self.subTest(value=value), self.assertRaises(
                    config_validation.ConfigValidationError):
                self._check(value)

    def test_the_example_config_has_the_key_with_the_default(self):
        text = Path("config.example.yaml").read_text()
        self.assertIn("STOP_GRACE_SECONDS: 15", text)
        self.assertEqual(notice.DEFAULT_STOP_GRACE_SECONDS, 15)

    def test_a_bad_value_at_run_time_gives_the_default(self):
        manager = mock.Mock()
        manager.get.return_value = "x"
        with mock.patch("src.utils.config.ConfigManager", return_value=manager):
            self.assertEqual(notice.stop_grace_seconds(), 15)
        manager.get.return_value = 0
        with mock.patch("src.utils.config.ConfigManager", return_value=manager):
            self.assertEqual(notice.stop_grace_seconds(), 0)


_LINUX_PTS = os.path.isdir("/dev/pts")


class _VmInfoServer:
    """A fake CH API socket that answers ``GET /api/v1/vm.info`` with ``body``."""

    def __init__(self, test, path, body, status=200):
        self.requests = []
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        test.addCleanup(server.close)

        def run():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    request = conn.recv(4096)
                    self.requests.append(request.split(b"\r\n", 1)[0])
                    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                    conn.sendall(
                        b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n" % (status, len(payload)) + payload)

        threading.Thread(target=run, daemon=True).start()


@skip
class CloudHypervisorPtyTests(unittest.TestCase):
    """The console of the notice on Cloud Hypervisor: ``--console pty`` (#645)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.dir.cleanup)
        self.api = os.path.join(self.dir.name, "ch.sock")

    def _pty(self):
        """A pty pair: the test holds the master, as CH does. Returns (master, slave path)."""
        import tty

        master, slave = os.openpty()
        tty.setraw(slave)  # CH makes the slave raw when it creates it
        path = os.ttyname(slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        os.set_blocking(master, False)
        return master, path

    @staticmethod
    def _read_all(fd, wait=0.5):
        data, end = b"", time.time() + wait
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
        return data

    def test_the_path_of_the_pty_comes_from_vm_info(self):
        server = _VmInfoServer(self, self.api, {"config": {"console": {"mode": "Pty", "file": "/dev/pts/7"}}})
        self.assertEqual(notice.ch_console_pty(self.api), "/dev/pts/7")
        self.assertEqual(server.requests, [b"GET /api/v1/vm.info HTTP/1.1"])

    def test_no_pty_in_vm_info_is_none(self):
        for console in ({"mode": "Off", "file": None}, {"mode": "Pty", "file": "/etc/passwd"},
                        {"mode": "Pty", "file": "/dev/pts/../../etc/x"}, {"mode": "Pty"}, None):
            with self.subTest(console=console):
                path = os.path.join(self.dir.name, f"s{len(os.listdir(self.dir.name))}.sock")
                _VmInfoServer(self, path, {"config": {"console": console}})
                self.assertIsNone(notice.ch_console_pty(path))

    def test_a_ch_that_does_not_answer_is_none_and_not_an_error(self):
        self.assertIsNone(notice.ch_console_pty(self.api))
        _VmInfoServer(self, self.api, b"not json")
        self.assertIsNone(notice.ch_console_pty(self.api))

    def test_an_error_status_is_none(self):
        _VmInfoServer(self, self.api, {"config": {"console": {"mode": "Pty", "file": "/dev/pts/7"}}}, status=500)
        self.assertIsNone(notice.ch_console_pty(self.api))

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_the_line_reaches_the_master_as_it_is_written(self):
        master, path = self._pty()
        self.assertTrue(notice.write_pty(path, notice.line(notice.STOP, 1700000000), owner_pid=os.getpid()))
        self.assertEqual(self._read_all(master), b"STOP 1700000000\n")

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_a_second_open_and_close_also_reaches_the_master(self):
        """The spike of #645 lost a line after a second open of the pty; the node opens it per line."""
        master, path = self._pty()
        self.assertTrue(notice.write_pty(path, notice.line(notice.STOP, 1), owner_pid=os.getpid()))
        self.assertTrue(notice.write_pty(path, notice.line(notice.HIBERNATE, 2), owner_pid=os.getpid()))
        self.assertEqual(self._read_all(master), b"STOP 1\nHIBERNATE 2\n")

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_a_slave_that_someone_made_cooked_is_made_raw_again(self):
        import termios
        master, path = self._pty()
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(fd)
        attrs[1] |= termios.OPOST | termios.ONLCR
        attrs[3] |= termios.ECHO | termios.ICANON
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.close(fd)
        self.assertTrue(notice.write_pty(path, notice.line(notice.STOP, 5), owner_pid=os.getpid()))
        self.assertEqual(self._read_all(master), b"STOP 5\n")  # no \r\n

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_what_the_guest_writes_is_thrown_away_with_a_limit_and_does_not_block(self):
        master, path = self._pty()
        written = 0
        try:
            while written < 1024 * 1024:
                written += os.write(master, b"x" * 4096)
        except BlockingIOError:
            pass
        started = time.time()
        with mock.patch.object(notice, "DISCARD_LIMIT_BYTES", 1024):
            self.assertTrue(notice.write_pty(path, notice.line(notice.STOP, 1), owner_pid=os.getpid()))
        self.assertLess(time.time() - started, 1.0)
        self.assertEqual(self._read_all(master), b"STOP 1\n")

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_the_pty_never_becomes_a_controlling_tty_of_the_node(self):
        master, path = self._pty()
        with mock.patch.object(notice.os, "open", wraps=os.open) as opened:
            self.assertTrue(notice.write_pty(path, notice.line(notice.STOP, 1), owner_pid=os.getpid()))
        flags = opened.call_args[0][1]
        self.assertTrue(flags & os.O_NOCTTY and flags & os.O_NONBLOCK)

    def test_a_path_that_is_not_a_pty_is_refused(self):
        regular = os.path.join(self.dir.name, "f")
        Path(regular).write_text("")
        self.assertFalse(notice.write_pty(regular, b"STOP 1\n", owner_pid=os.getpid()))
        self.assertFalse(notice.write_pty("/dev/pts/999999", b"STOP 1\n", owner_pid=os.getpid()))
        self.assertEqual(Path(regular).read_text(), "")

    def test_has_device_reads_the_runtime_state_of_a_ch_vm(self):
        missing = os.path.join(self.dir.name, "none.sock")
        with mock.patch.object(notice, "socket_path", return_value=missing):
            for state, expected in (
                ({"virtualizer": "ch", "control_socket": self.api, "notice_console": "pty"}, True),
                ({"virtualizer": "ch", "control_socket": self.api}, False),
                (None, False),
            ):
                with self.subTest(state=state), \
                        mock.patch.object(notice, "_runtime_state", return_value=state):
                    self.assertEqual(notice.has_device("x"), expected)

    def test_deliver_finds_the_pty_again_at_each_notice(self):
        """Nothing is kept between notices: a restart of the node finds the pty the same way."""
        state = {"virtualizer": "ch", "control_socket": self.api, "notice_console": "pty", "pid": 4242}
        missing = os.path.join(self.dir.name, "none.sock")
        with mock.patch.object(notice, "socket_path", return_value=missing), \
                mock.patch.object(notice, "_runtime_state", return_value=state), \
                mock.patch.object(notice, "ch_console_pty", side_effect=["/dev/pts/3", "/dev/pts/9"]) as found, \
                mock.patch.object(notice, "write_pty", return_value=True) as wrote:
            self.assertTrue(notice.deliver("x", notice.STOP, 10))
            self.assertTrue(notice.deliver("x", notice.HIBERNATE, 20))
        self.assertEqual(found.call_count, 2)
        self.assertEqual([c.args for c in wrote.call_args_list],
                         [("/dev/pts/3", b"STOP 10\n"), ("/dev/pts/9", b"HIBERNATE 20\n")])
        self.assertEqual([c.kwargs for c in wrote.call_args_list],
                         [{"owner_pid": 4242}, {"owner_pid": 4242}])

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_owns_pty_is_true_for_the_holder_of_the_master_only(self):
        master, path = self._pty()
        self.assertTrue(notice.owns_pty(os.getpid(), path))
        other, other_path = self._pty()
        self.assertTrue(notice.owns_pty(os.getpid(), other_path))
        self.assertFalse(notice.owns_pty(os.getpid(), "/dev/pts/999999"))
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.kill)
        self.assertFalse(notice.owns_pty(child.pid, path))  # holds no master
        self.assertFalse(notice.owns_pty(2 ** 22 + 1, path))  # no such process

    @unittest.skipUnless(_LINUX_PTS, "needs /dev/pts")
    def test_a_pty_that_the_ch_process_does_not_own_gets_nothing(self):
        """A CH that a guest took over names the pty of another session in ``vm.info``."""
        import termios
        master, path = self._pty()
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.kill)
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        before = termios.tcgetattr(fd)
        os.close(fd)
        self.assertFalse(notice.write_pty(path, notice.line(notice.STOP, 1), owner_pid=child.pid))
        self.assertEqual(self._read_all(master), b"")  # nothing written
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self.assertEqual(termios.tcgetattr(fd), before)  # not made raw again
        os.close(fd)

    def test_deliver_with_no_pid_for_ch_is_false(self):
        state = {"virtualizer": "ch", "control_socket": self.api, "notice_console": "pty"}
        missing = os.path.join(self.dir.name, "none.sock")
        with mock.patch.object(notice, "socket_path", return_value=missing), \
                mock.patch.object(notice, "_runtime_state", return_value=state), \
                mock.patch.object(notice, "ch_console_pty", return_value="/dev/pts/3"), \
                mock.patch.object(notice, "write_pty") as wrote:
            self.assertFalse(notice.deliver("x", notice.STOP, 10))
        wrote.assert_not_called()

    def test_deliver_with_no_pty_is_false(self):
        state = {"virtualizer": "ch", "control_socket": self.api, "notice_console": "pty", "pid": 4242}
        missing = os.path.join(self.dir.name, "none.sock")
        with mock.patch.object(notice, "socket_path", return_value=missing), \
                mock.patch.object(notice, "_runtime_state", return_value=state), \
                mock.patch.object(notice, "write_pty") as wrote:
            self.assertFalse(notice.deliver("x", notice.STOP, 10))
        wrote.assert_not_called()
        with mock.patch.object(notice, "socket_path", return_value=missing), \
                mock.patch.object(notice, "_runtime_state", return_value={}):
            self.assertFalse(notice.deliver("x", notice.STOP, 10))


@skip
class HypervisorArgumentTests(unittest.TestCase):
    def test_ch_without_the_declaration_keeps_the_console_off(self):
        args, _ = ch._resolve_ch_stream_args(runtime_dir=Path("/run/nodo/vm-a"))
        self.assertEqual(args[args.index("--console") + 1], "off")

    def test_ch_with_the_declaration_gets_a_pty_and_keeps_the_serial_log(self):
        """CH v51.1 refuses ``--console socket=``: the console of the notice is a pty."""
        args, serial = ch._resolve_ch_stream_args(
            runtime_dir=Path("/run/nodo/vm-a"), notice_console=True)
        self.assertEqual(args[args.index("--console") + 1], "pty")
        self.assertNotIn("socket=", " ".join(args))
        self.assertIn(f"file={serial}", args)

    def _qemu(self, **kw):
        return qemu_exec.build_qemu_command(
            qemu_binary="qemu-system-x86_64", arch="linux/amd64", kernel_path="/k",
            initramfs_path="/i", rootfs_path=Path("/r"), vcpus=1, mem_mib=256, tap_name="tap0",
            mac="02:00:00:00:00:01", cmdline="console=ttyS0", serial_log_path=Path("/s"), **kw)

    def test_qemu_without_the_declaration_has_no_virtio_console(self):
        argv = self._qemu()
        self.assertNotIn("virtconsole,chardev=notice0", argv)
        self.assertEqual(argv[argv.index("-serial") + 1], "file:/s")

    def test_qemu_with_the_declaration_has_a_virtconsole_next_to_the_serial_port(self):
        argv = self._qemu(notice_socket_path="/run/nodo/ch/notice-x.sock")
        self.assertIn("virtconsole,chardev=notice0", argv)
        self.assertIn("virtio-serial-pci,id=noticeserial0", argv)
        self.assertIn("socket,id=notice0,path=/run/nodo/ch/notice-x.sock,server=on,wait=off", argv)
        self.assertEqual(argv[argv.index("-serial") + 1], "file:/s")


@skip
class StoppingTestCase(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(lambda: (connection.close(), setattr(SQLConnection, "_connection", original)))
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.7", container_id="a",
            name="web", balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50,
            envs="{}",
        )
        self.sent = []
        patches = [
            mock.patch.object(stopping.notice, "has_device", return_value=True),
            mock.patch.object(stopping.notice, "stop_grace_seconds", return_value=15),
            mock.patch.object(stopping.notice, "deliver",
                              side_effect=lambda token, verb, deadline: self.sent.append(
                                  (verb, int(deadline))) or True),
            mock.patch.object(stopping, "vm_has_ended", return_value=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


@skip
class BeginNoticeTests(StoppingTestCase):
    def test_a_notice_marks_the_row_and_writes_the_line(self):
        with mock.patch.object(stopping.time, "time", return_value=1000.0):
            deadline = stopping.begin_notice("a", stopping.STOP, dead_instances.FATHER_STOP)
        self.assertEqual(deadline, 1015.0)
        self.assertEqual(self.sent, [("STOP", 1015)])
        row = self.sc.get_local_stopping("a")
        self.assertEqual(row["until"], 1015.0)
        self.assertEqual(row["kind"], "stop")
        self.assertEqual(row["reason"], dead_instances.FATHER_STOP)

    def test_a_second_request_joins_the_first_and_writes_nothing(self):
        first = stopping.begin_notice("a", stopping.STOP, dead_instances.FATHER_STOP)
        second = stopping.begin_notice("a", stopping.STOP, dead_instances.ACTIVITY_WINDOW)
        self.assertEqual(first, second)
        self.assertEqual(len(self.sent), 1)

    def test_no_notice_when_the_wait_is_zero(self):
        with mock.patch.object(stopping.notice, "stop_grace_seconds", return_value=0):
            self.assertIsNone(stopping.begin_notice("a", stopping.STOP, "father_stop"))
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.sc.get_local_stopping("a"))

    def test_no_notice_when_the_vm_has_no_device(self):
        with mock.patch.object(stopping.notice, "has_device", return_value=False):
            self.assertIsNone(stopping.begin_notice("a", stopping.STOP, "father_stop"))
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.sc.get_local_stopping("a"))

    def test_no_notice_for_an_instance_that_is_not_a_local_one(self):
        self.assertIsNone(stopping.begin_notice("delegated", stopping.STOP, "father_stop"))

    def test_a_line_that_cannot_be_written_still_gives_the_whole_wait(self):
        """The service declared a notice: a line that did not arrive is no reason to stop it
        before the limit. The row stays marked, and the caller waits."""
        with mock.patch.object(stopping.time, "time", return_value=1000.0), \
                mock.patch.object(stopping.notice, "deliver", return_value=False):
            deadline = stopping.begin_notice("a", stopping.STOP, "father_stop")
        self.assertEqual(deadline, 1015.0)
        self.assertEqual(self.sc.get_local_stopping("a")["until"], 1015.0)

    def test_a_failure_of_the_notice_never_keeps_the_node_from_stopping(self):
        with mock.patch.object(stopping.notice, "has_device", side_effect=RuntimeError("x")):
            self.assertIsNone(stopping.begin_notice("a", stopping.STOP, "father_stop"))
        self.assertIsNone(self.sc.get_local_stopping("a"))

    def test_the_hibernation_writes_its_own_verb(self):
        stopping.begin_notice("a", stopping.HIBERNATE, dead_instances.HIBERNATED)
        self.assertEqual(self.sent[0][0], "HIBERNATE")

    def test_a_suspended_instance_gets_no_notice(self):
        self.sc.hibernate_local_instance("a")
        self.assertIsNone(stopping.begin_notice("a", stopping.STOP, "father_stop"))


@skip
class StopWithNoticeTests(StoppingTestCase):
    def test_it_waits_until_the_limit_and_then_stops_as_before(self):
        clock = {"now": 1000.0}

        def sleep(seconds):
            clock["now"] += seconds

        with mock.patch.object(stopping.time, "time", side_effect=lambda: clock["now"]), \
                mock.patch.object(stopping.time, "sleep", side_effect=sleep), \
                mock.patch.object(stopping, "stop_instance", return_value=42) as stop:
            refund = stopping.stop_with_notice("a", reason="operator_kill")
        self.assertEqual(refund, 42)
        self.assertGreaterEqual(clock["now"], 1015.0)
        self.assertEqual(self.sent, [("STOP", 1015)])
        stop.assert_called_once_with(token="a", reason="operator_kill")

    def test_a_line_that_cannot_be_written_does_not_stop_it_before_the_limit(self):
        clock = {"now": 1000.0}

        def sleep(seconds):
            clock["now"] += seconds

        with mock.patch.object(stopping.time, "time", side_effect=lambda: clock["now"]), \
                mock.patch.object(stopping.time, "sleep", side_effect=sleep), \
                mock.patch.object(stopping.notice, "deliver", return_value=False), \
                mock.patch.object(stopping, "stop_instance", return_value=3) as stop:
            self.assertEqual(stopping.stop_with_notice("a"), 3)
        self.assertGreaterEqual(clock["now"], 1015.0)
        stop.assert_called_once()

    def test_the_wait_ends_early_when_the_vm_ends(self):
        clock = {"now": 1000.0}

        def sleep(seconds):
            clock["now"] += seconds

        with mock.patch.object(stopping.time, "time", side_effect=lambda: clock["now"]), \
                mock.patch.object(stopping.time, "sleep", side_effect=sleep), \
                mock.patch.object(stopping, "vm_has_ended", side_effect=[False, False, True]), \
                mock.patch.object(stopping, "stop_instance", return_value=7) as stop:
            self.assertEqual(stopping.stop_with_notice("a"), 7)
        self.assertLess(clock["now"], 1015.0)
        stop.assert_called_once()

    def test_the_stop_is_at_once_with_no_declaration(self):
        with mock.patch.object(stopping.notice, "has_device", return_value=False), \
                mock.patch.object(stopping.time, "sleep") as sleep, \
                mock.patch.object(stopping, "stop_instance", return_value=5) as stop:
            self.assertEqual(stopping.stop_with_notice("a"), 5)
        sleep.assert_not_called()
        stop.assert_called_once()

    def test_the_wait_does_not_hold_the_lock_of_the_instance(self):
        held = []

        def observe(token):
            held.append(stopping.instance_lock)  # the module still uses the lock only in begin
            return False

        taken = []
        real = stopping.instance_lock

        def tracking(token):
            class _Ctx:
                def __enter__(self_inner):
                    taken.append("in")
                    return real(token).__enter__()

                def __exit__(self_inner, *a):
                    taken.append("out")
                    return False
            return _Ctx()

        with mock.patch.object(stopping, "instance_lock", side_effect=tracking), \
                mock.patch.object(stopping, "vm_has_ended",
                                  side_effect=lambda t: taken.append("wait") or True), \
                mock.patch.object(stopping, "stop_instance", return_value=1):
            stopping.stop_with_notice("a")
        self.assertEqual(taken, ["in", "out", "wait"])

    def test_a_stop_that_the_tick_ended_first_is_not_an_error(self):
        with mock.patch.object(stopping, "vm_has_ended", return_value=True), \
                mock.patch.object(stopping, "stop_instance", return_value=None), \
                mock.patch.object(stopping.sc, "internal_instance_exists",
                                  side_effect=[True, False]):
            self.assertEqual(stopping.stop_with_notice("a"), 0)


@skip
class RequestTests(StoppingTestCase):
    """What a father asks through the gateway: mark, return, and let the tick end it."""

    def test_a_stop_marks_the_instance_returns_at_once_and_refunds_nothing_yet(self):
        with mock.patch.object(stopping.time, "time", return_value=1000.0), \
                mock.patch.object(stopping.time, "sleep") as sleep, \
                mock.patch.object(stopping, "stop_instance") as stop:
            self.assertEqual(stopping.request_stop("a", reason="father_stop"), 0)
        sleep.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(self.sent, [("STOP", 1015)])
        row = self.sc.get_local_stopping("a")
        self.assertEqual((row["until"], row["kind"]), (1015.0, "stop"))

    def test_the_tick_ends_the_stop_that_a_request_marked(self):
        with mock.patch.object(stopping.time, "time", return_value=1000.0):
            stopping.request_stop("a", reason="father_stop")
        with mock.patch.object(stopping, "stop_instance") as stop:
            self.assertEqual(stopping.finish_due_stops(now=1014.0), 0)
            self.assertEqual(stopping.finish_due_stops(now=1015.0), 1)
        stop.assert_called_once_with(token="a", reason="father_stop")  # the father is credited

    def test_a_second_stop_joins_the_first_and_writes_nothing(self):
        stopping.request_stop("a")
        self.assertEqual(stopping.request_stop("a"), 0)
        self.assertEqual(len(self.sent), 1)

    def test_a_stop_with_no_declaration_is_at_once_and_returns_the_refund(self):
        with mock.patch.object(stopping.notice, "has_device", return_value=False), \
                mock.patch.object(stopping, "stop_instance", return_value=42) as stop:
            self.assertEqual(stopping.request_stop("a", reason="father_stop"), 42)
        stop.assert_called_once_with(token="a", reason="father_stop")
        self.assertEqual(self.sent, [])

    def test_a_hibernation_marks_the_instance_and_returns_at_once(self):
        with mock.patch.object(hibernation, "check_can_hibernate"), \
                mock.patch.object(stopping.time, "sleep") as sleep, \
                mock.patch.object(hibernation, "hibernate_instance") as hib:
            self.assertTrue(stopping.request_hibernation("a"))
        sleep.assert_not_called()
        hib.assert_not_called()
        self.assertEqual(self.sent[0][0], "HIBERNATE")
        self.assertEqual(self.sc.get_local_stopping("a")["kind"], "hibernate")

    def test_a_hibernation_with_no_declaration_is_at_once(self):
        with mock.patch.object(stopping.notice, "has_device", return_value=False), \
                mock.patch.object(hibernation, "check_can_hibernate"), \
                mock.patch.object(hibernation, "hibernate_instance", return_value=True) as hib:
            self.assertTrue(stopping.request_hibernation("a"))
        hib.assert_called_once_with("a", reason=dead_instances.HIBERNATED)
        self.assertEqual(self.sent, [])

    def test_a_refusal_comes_before_any_notice(self):
        with mock.patch.object(hibernation, "check_can_hibernate",
                               side_effect=hibernation.HibernationRefused("shares")):
            with self.assertRaises(hibernation.HibernationRefused):
                stopping.request_hibernation("a")
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.sc.get_local_stopping("a"))

    def test_the_mark_goes_when_the_instance_is_hibernated(self):
        stopping.begin_notice("a", stopping.HIBERNATE, dead_instances.HIBERNATED)
        self.assertIsNotNone(self.sc.get_local_stopping("a"))
        self.sc.hibernate_local_instance("a")
        self.assertIsNone(self.sc.get_local_stopping("a"))


@skip
class TickTests(StoppingTestCase):
    def test_the_tick_ends_a_stop_at_the_limit(self):
        stopping.begin_notice("a", stopping.STOP, dead_instances.ACTIVITY_WINDOW)
        until = self.sc.get_local_stopping("a")["until"]
        with mock.patch.object(stopping, "stop_instance") as stop:
            self.assertEqual(stopping.finish_due_stops(now=until - 1), 0)
            stop.assert_not_called()
            self.assertEqual(stopping.finish_due_stops(now=until), 1)
        stop.assert_called_once_with(
            token="a", reason=dead_instances.ACTIVITY_WINDOW)

    def test_a_stop_always_credits_the_father(self):
        """No row says otherwise: only ``nodo burnall`` stops with ``credit=False``, and it
        gives no notice."""
        stopping.begin_notice("a", stopping.STOP, dead_instances.FATHER_STOP)
        columns = [r["name"] for r in self.sc._execute("PRAGMA table_info(local_instances)").fetchall()]
        self.assertNotIn("stopping_credit", columns)
        with mock.patch.object(stopping, "stop_instance") as stop:
            stopping.finish_due_stops(now=10 ** 12)
        self.assertNotIn("credit", stop.call_args.kwargs)  # the default of stop_instance: True

    def test_the_tick_ends_a_stop_when_the_vm_ended(self):
        stopping.begin_notice("a", stopping.STOP, dead_instances.ACTIVITY_WINDOW)
        with mock.patch.object(stopping, "vm_has_ended", return_value=True), \
                mock.patch.object(stopping, "stop_instance") as stop:
            self.assertEqual(stopping.finish_due_stops(now=0), 1)
        stop.assert_called_once()

    def test_the_tick_hibernates_what_the_row_says(self):
        stopping.begin_notice("a", stopping.HIBERNATE, dead_instances.HIBERNATED_BY_WINDOW)
        until = self.sc.get_local_stopping("a")["until"]
        with mock.patch.object(hibernation, "hibernate_instance", return_value=True) as hib, \
                mock.patch.object(stopping, "stop_instance") as stop:
            stopping.finish_due_stops(now=until + 100)
        hib.assert_called_once_with("a", reason=dead_instances.HIBERNATED_BY_WINDOW)
        stop.assert_not_called()

    def test_a_hibernation_that_is_refused_at_the_end_is_a_stop(self):
        stopping.begin_notice("a", stopping.HIBERNATE, dead_instances.HIBERNATED_BY_WINDOW)
        until = self.sc.get_local_stopping("a")["until"]
        with mock.patch.object(hibernation, "hibernate_instance",
                               side_effect=hibernation.HibernationRefused("x")), \
                mock.patch.object(stopping, "stop_instance") as stop:
            stopping.finish_due_stops(now=until + 100)
        stop.assert_called_once_with(token="a", reason=dead_instances.ACTIVITY_WINDOW)

    def test_a_hibernation_that_fails_for_any_reason_is_a_stop(self):
        """Not a row that stays marked for the tick to try again at each pass."""
        stopping.begin_notice("a", stopping.HIBERNATE, dead_instances.HIBERNATED_BY_WINDOW)
        until = self.sc.get_local_stopping("a")["until"]
        with mock.patch.object(hibernation, "hibernate_instance",
                               side_effect=OSError("no space left")), \
                mock.patch.object(stopping, "stop_instance") as stop:
            self.assertEqual(stopping.finish_due_stops(now=until + 100), 1)
        stop.assert_called_once_with(token="a", reason=dead_instances.ACTIVITY_WINDOW)

    def test_a_failure_does_not_take_the_tick_down(self):
        stopping.begin_notice("a", stopping.STOP, dead_instances.FATHER_STOP)
        with mock.patch.object(stopping, "stop_instance", side_effect=RuntimeError("busy")):
            self.assertEqual(stopping.finish_due_stops(now=10 ** 12), 1)

    def test_a_stopping_instance_is_not_scored_as_lost_and_is_not_revived(self):
        """The VM ends by itself (PID 1 exits and the guest panics): the stop ends, with no
        penalty and no revive."""
        stopping.begin_notice("a", stopping.STOP, dead_instances.FATHER_STOP)

        def vm_maintain(vmachine_id, debug_mode, remove_and_penalize):
            remove_and_penalize(vmachine_id=vmachine_id, reason=dead_instances.GUEST_PANIC)

        with mock.patch.object(maintain, "vm_maintain", side_effect=vm_maintain), \
                mock.patch.object(maintain, "_penalise_instance_once") as penalise, \
                mock.patch.object(maintain, "handle_internal_failure") as failure, \
                mock.patch.object(maintain.stopping, "finish_stop") as finish, \
                mock.patch.object(maintain, "system_scarcity"), \
                mock.patch.object(maintain.price_history, "record"):
            maintain.maintain_vmachines()
        penalise.assert_not_called()
        failure.assert_not_called()
        finish.assert_called_once()
        self.assertEqual(finish.call_args[0][0], "a")

    def test_an_instance_that_is_not_stopping_is_still_scored_as_lost(self):
        def vm_maintain(vmachine_id, debug_mode, remove_and_penalize):
            remove_and_penalize(vmachine_id=vmachine_id)

        with mock.patch.object(maintain, "vm_maintain", side_effect=vm_maintain), \
                mock.patch.object(maintain, "_penalise_instance_once") as penalise, \
                mock.patch.object(maintain, "handle_internal_failure"), \
                mock.patch.object(maintain, "system_scarcity"), \
                mock.patch.object(maintain.price_history, "record"):
            maintain.maintain_vmachines()
        penalise.assert_called_once()


@skip
class WindowTests(StoppingTestCase):
    """The window cannot wait in the tick: with a notice, the stop ends in a later tick."""

    def _close(self, policy):
        with mock.patch.object(maintain.activity_window, "is_open", return_value=False), \
                mock.patch.object(maintain.activity_window, "on_close_policy",
                                  return_value=policy), \
                mock.patch.object(maintain.activity_window, "force_actual_on_close_policy",
                                  return_value=True), \
                mock.patch.object(maintain, "descends_from_dev_client", return_value=False), \
                mock.patch.object(maintain, "stop_instance") as stop, \
                mock.patch.object(hibernation, "check_can_hibernate"), \
                mock.patch.object(hibernation, "hibernate_instance") as hib:
            maintain.enforce_activity_window()
            maintain.enforce_activity_window()  # the next tick does not write it again
        return stop, hib

    def test_stop_writes_the_notice_and_leaves_the_stop_to_the_tick(self):
        stop, hib = self._close("stop")
        self.assertEqual(self.sent, [("STOP", self.sent[0][1])])
        self.assertEqual(len(self.sent), 1)
        stop.assert_not_called()
        self.assertEqual(self.sc.get_local_stopping("a")["reason"], dead_instances.ACTIVITY_WINDOW)

    def test_hibernate_writes_the_notice_and_leaves_the_hibernation_to_the_tick(self):
        stop, hib = self._close("hibernate")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "HIBERNATE")
        hib.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(self.sc.get_local_stopping("a")["kind"], "hibernate")

    def test_the_window_puts_the_instances_down_together(self):
        """Each notice writes to a VM and may take seconds: the tick does not add them."""
        for number in range(2, 5):
            self.sc.add_local_instance(
                father_id="father", container_ip=f"192.168.200.{number + 6}",
                container_id=str(number), name=f"web{number}", balance_mu=100,
                serialized_instance="x", service_id="svc", virtualizer="ch",
                mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50, envs="{}")
        ids = self.sc.get_all_internal_containers_ids()
        self.assertEqual(len(ids), 4)
        barrier = threading.Barrier(4, timeout=5)

        def deliver(token, verb, deadline):
            barrier.wait()  # only passes if the four notices run at the same time
            return True

        with mock.patch.object(stopping.notice, "deliver", side_effect=deliver):
            stop, hib = self._close("stop")
        self.assertEqual(sorted(self.sc.get_stopping_local_instances()), sorted(ids))
        stop.assert_not_called()

    def test_a_failure_of_one_instance_does_not_keep_the_window_from_the_others(self):
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.9", container_id="b",
            name="web-b", balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100,
            cpu_quota=50, envs="{}")
        calls = []

        def begin(token, kind, reason, *args, **kwargs):
            calls.append(token)
            if token == "a":
                raise RuntimeError("boom")
            return 1.0

        with mock.patch.object(stopping, "begin_notice", side_effect=begin):
            self._close("stop")
        self.assertIn("b", calls)

    def test_with_no_device_the_window_stops_at_once_as_before(self):
        with mock.patch.object(stopping.notice, "has_device", return_value=False):
            stop, hib = self._close("stop")
        self.assertEqual(self.sent, [])
        self.assertEqual(stop.call_count, 2)  # at once, and again at the next tick, as before
        stop.assert_called_with(token="a", reason=dead_instances.ACTIVITY_WINDOW)


@skip
class CallerTableTests(unittest.TestCase):
    """Who writes a notice, and who stops at once (section 5 of #645)."""

    def test_the_callers_that_get_no_notice_call_the_plain_functions(self):
        """burnall and the callers of the tick (out_of_balance, process_lost) stop at once."""
        import inspect
        from src.commands import burnall
        source = inspect.getsource(burnall)
        self.assertNotIn("stop_with_notice", source)
        self.assertNotIn("begin_notice", source)
        self.assertIn("stop_instance(token=i, credit=False", source)
        for name in ("handle_internal_failure", "reap_expired_suspensions"):
            self.assertNotIn("begin_notice", inspect.getsource(getattr(maintain, name)))

    def test_stop_service_marks_and_kill_waits(self):
        """The gRPC server holds no thread for the wait; the CLI process of ``nodo kill`` does."""
        import inspect
        from src.commands import kill
        from src.gateway import gateway
        source = inspect.getsource(gateway.Gateway.StopService)
        self.assertIn("request_stop", source)
        self.assertNotIn("stop_with_notice", source)
        self.assertIn("stop_with_notice", inspect.getsource(kill.kill))

    def test_a_father_that_hibernates_marks_and_the_instance_that_hibernates_itself_does_not(self):
        from protos import celaut_pb2 as pb
        from src.gateway import gateway
        request = pb.ModifyServiceSystemResourcesInput(
            service_token="a", max_sysreq=pb.Sysresources(cpu_quota=0))
        context = mock.Mock(peer=lambda: "ipv4:10.0.0.9:5555")
        for token, expected in (("a", "with_notice"), ("", "plain")):
            request.service_token = token
            called = []
            with mock.patch.object(hibernation, "check_can_hibernate"), \
                    mock.patch.object(hibernation, "plan", return_value=hibernation.HIBERNATE), \
                    mock.patch.object(hibernation, "hibernated_view", return_value=pb.Refund()), \
                    mock.patch.object(hibernation, "hibernate_instance",
                                      side_effect=lambda *a, **k: called.append("plain")), \
                    mock.patch.object(gateway.stopping, "request_hibernation",
                                      side_effect=lambda *a, **k: called.append("with_notice")), \
                    mock.patch.object(gateway, "parse_with_client", return_value=(request, "")), \
                    mock.patch.object(gateway, "require_father"), \
                    mock.patch.object(gateway, "get_internal_service_id_by_uri", return_value="a"), \
                    mock.patch.object(gateway, "get_only_the_ip_from_context", return_value="1.1.1.1"), \
                    mock.patch.object(gateway.sc, "internal_instance_exists", return_value=True), \
                    mock.patch.object(gateway, "spend_mu", return_value=True), \
                    mock.patch.object(gateway.BeeClient, "respond",
                                      side_effect=lambda message_iterator: iter([message_iterator])):
                list(gateway.Gateway.ModifyServiceSystemResources(mock.Mock(), iter(()), context))
            self.assertEqual(called, [expected], token)

    def test_stop_service_answers_with_the_refund_of_the_request_and_does_not_wait(self):
        from protos import celaut_pb2 as pb
        from src.gateway import gateway
        for refund in (0, 42):
            with mock.patch.object(gateway, "parse_with_client",
                                   return_value=(pb.TokenMessage(token="a"), "")), \
                    mock.patch.object(gateway, "require_father"), \
                    mock.patch.object(gateway.stopping, "request_stop", return_value=refund) as req, \
                    mock.patch.object(gateway.stopping, "wait_until") as wait, \
                    mock.patch.object(gateway.BeeClient, "respond",
                                      side_effect=lambda message_iterator: iter([message_iterator])):
                out = list(gateway.Gateway.StopService(mock.Mock(), iter(()), mock.Mock()))
            req.assert_called_once_with(token="a", reason="father_stop")
            wait.assert_not_called()
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0], pb.Refund(amount=gateway.to_amount(refund)))


if __name__ == "__main__":
    unittest.main()


class StopReasonTests(StoppingTestCase):
    """Who asked for a stop is in its reason, so `nodo kill` and a father are told apart."""

    def test_a_father_request_records_father_stop(self):
        with mock.patch.object(stopping, "stop_instance", return_value=7) as stop:
            stopping.request_stop("nonlocal")
        stop.assert_called_once_with(token="nonlocal", reason="father_stop")

    def test_the_operator_kill_records_operator_kill(self):
        with mock.patch.object(stopping, "stop_instance", return_value=7) as stop:
            stopping.stop_with_notice("nonlocal")
        stop.assert_called_once_with(token="nonlocal", reason="operator_kill")

    def test_stop_instance_has_no_default_reason(self):
        import inspect
        from src.manager import manager
        self.assertIs(inspect.signature(manager.stop_instance).parameters["reason"].default,
                      inspect.Parameter.empty)
