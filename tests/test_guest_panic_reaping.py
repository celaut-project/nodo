"""A live hypervisor is not a live guest.

A panicking Linux kernel never exits: with no ``panic=`` timeout it spins in
place, so the hypervisor process stays alive, its control socket keeps
answering, and under TCG its vCPU thread keeps a host core busy. Every liveness
test that asks about the process reports that VM healthy, and it stays listed,
billed and resident until an operator notices.

Observed on this node: a `heavy` guest asked for 300 MiB against a 256 MiB
ceiling. Its entrypoint is PID 1, so the guest kernel had nothing killable and
panicked with "Attempted to kill init! exitcode=0x00000005". The QEMU process
then held 99.9% of a core for 27 minutes while `maintain` reported it healthy
every tick.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.ch import guest_panic
    from src.virtualizers.ch import maintain as ch_maintain
    from src.virtualizers.qemu import maintain as qemu_maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    guest_panic = ch_maintain = qemu_maintain = None  # type: ignore[assignment]


# The tail of the real serial log, verbatim (\r\n line endings included: this is
# a serial console, and a matcher that only handles \n silently sees nothing).
PANIC_TAIL = (
    "[    4.906204] __vm_enough_memory: pid: 97, comm: tokio-runtime-w, bytes: "
    "314576896 not enough memory for the allocation\r\n"
    "memory allocation of 314572800 bytes failed\r\n"
    "[    4.916739] Kernel panic - not syncing: Attempted to kill init! "
    "exitcode=0x00000005\r\n"
    "[    4.927211] ---[ end Kernel panic - not syncing: Attempted to kill init! "
    "exitcode=0x00000005 ]---\r\n"
)

HEALTHY_TAIL = (
    "+ exec switch_root /newroot /heavy-service\r\n"
    "Starting the HEAVY service (verifier-extended version)...\r\n"
    "HEAVY Service listening on http://0.0.0.0:3030\r\n"
)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestPanicDetectionTests(unittest.TestCase):
    def _state_with(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".serial.log", delete=False, newline="")
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return {"serial_log": fh.name}

    def test_a_panic_is_read_off_the_serial_log(self):
        line = guest_panic.guest_panic_line(self._state_with(PANIC_TAIL))
        self.assertEqual(
            line, "Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000005"
        )

    def test_a_healthy_guest_reports_nothing(self):
        self.assertIsNone(guest_panic.guest_panic_line(self._state_with(HEALTHY_TAIL)))

    def test_a_panic_without_printk_timestamps_is_still_found(self):
        state = self._state_with("Kernel panic - not syncing: out of memory\r\n")
        self.assertIsNotNone(guest_panic.guest_panic_line(state))

    def test_service_output_mentioning_the_words_does_not_trip_it(self):
        # The marker has to start a line. A log line *about* panics is not one.
        state = self._state_with("recovered from what looked like a Kernel panic - not syncing\r\n")
        self.assertIsNone(guest_panic.guest_panic_line(state))

    def test_a_log_that_cannot_be_read_is_not_a_panic(self):
        # Absent evidence must not reap: a missing log would otherwise kill every
        # healthy guest whose runtime directory was cleaned early.
        self.assertIsNone(guest_panic.guest_panic_line({"serial_log": "/nonexistent"}))
        self.assertIsNone(guest_panic.guest_panic_line({}))
        self.assertIsNone(guest_panic.guest_panic_line(None))

    def test_only_the_tail_is_read(self):
        # A panic is the last thing a kernel prints, so the window is bounded --
        # a long-running guest's console must not be read in full every tick.
        state = self._state_with(("filler line\r\n" * 20000) + PANIC_TAIL)
        self.assertGreater(os.path.getsize(state["serial_log"]), guest_panic.SERIAL_TAIL_BYTES)
        self.assertIsNotNone(guest_panic.guest_panic_line(state))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PanickedGuestIsReapedTests(unittest.TestCase):
    """The process outlives the guest, so it has to be killed here."""

    STATE = {"pid": 555, "serial_log": "/irrelevant-mocked"}

    def _run_ch(self, panic_line):
        removed = []
        with patch.object(ch_maintain, "load_runtime_state", return_value=dict(self.STATE)), \
             patch.object(ch_maintain, "pid_alive", return_value=True), \
             patch.object(ch_maintain, "guest_panic_line", return_value=panic_line), \
             patch.object(ch_maintain, "kill_ch_vm", return_value=True) as kill_mock:
            ch_maintain.maintain(
                vmachine_id="vm-1",
                debug_mode=False,
                remove_and_penalize=lambda vmachine_id: removed.append(vmachine_id),
            )
        return kill_mock, removed

    def _run_qemu(self, panic_line):
        removed = []
        with patch.object(qemu_maintain, "load_runtime_state", return_value=dict(self.STATE)), \
             patch.object(qemu_maintain, "pid_alive", return_value=True), \
             patch.object(qemu_maintain, "guest_panic_line", return_value=panic_line), \
             patch("src.virtualizers.qemu.kill.kill", return_value=True) as kill_mock:
            qemu_maintain.maintain(
                vmachine_id="vm-1",
                debug_mode=False,
                remove_and_penalize=lambda vmachine_id: removed.append(vmachine_id),
            )
        return kill_mock, removed

    def test_ch_kills_the_hypervisor_of_a_panicked_guest(self):
        kill_mock, removed = self._run_ch("Kernel panic - not syncing: x")
        kill_mock.assert_called_once_with(vmachine_id="vm-1")
        self.assertEqual(removed, ["vm-1"])

    def test_qemu_kills_the_emulator_of_a_panicked_guest(self):
        kill_mock, removed = self._run_qemu("Kernel panic - not syncing: x")
        kill_mock.assert_called_once_with(vmachine_id="vm-1")
        self.assertEqual(removed, ["vm-1"])

    def test_a_healthy_guest_is_left_running(self):
        for runner in (self._run_ch, self._run_qemu):
            kill_mock, removed = runner(None)
            kill_mock.assert_not_called()
            self.assertEqual(removed, [])

    def test_teardown_still_runs_when_the_kill_itself_fails(self):
        # A kill that raises must not strand the row and the deposit with it.
        removed = []
        with patch.object(ch_maintain, "load_runtime_state", return_value=dict(self.STATE)), \
             patch.object(ch_maintain, "pid_alive", return_value=True), \
             patch.object(ch_maintain, "guest_panic_line", return_value="Kernel panic - not syncing: x"), \
             patch.object(ch_maintain, "kill_ch_vm", side_effect=OSError("boom")):
            ch_maintain.maintain(
                vmachine_id="vm-1",
                debug_mode=False,
                remove_and_penalize=lambda vmachine_id: removed.append(vmachine_id),
            )
        self.assertEqual(removed, ["vm-1"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PanickedGuestIsAnOrphanTests(unittest.TestCase):
    """`orphan_reason` is the one definition of orphan, so the janitor and
    `nodo prune` both have to see a panicked guest as one."""

    def _reason(self, state, in_db=True, panic="Kernel panic - not syncing: x"):
        with patch.object(ch_maintain.sc, "internal_instance_exists", return_value=in_db), \
             patch.object(ch_maintain, "pid_alive", return_value=True), \
             patch.object(ch_maintain, "guest_panic_line", return_value=panic):
            return ch_maintain.orphan_reason(vmachine_id="vm-1", state=state)

    def test_a_panicked_guest_is_an_orphan(self):
        self.assertEqual(self._reason({"pid": 555}), "guest_panicked")

    def test_a_healthy_guest_is_not(self):
        self.assertIsNone(self._reason({"pid": 555}, panic=None))

    def test_a_booting_guest_is_left_to_its_launcher(self):
        # Same grace the row check gets: the launcher is still driving that VM
        # and has its own deadline for giving up on it.
        self.assertIsNone(self._reason({"pid": 555, "booting": True}))
