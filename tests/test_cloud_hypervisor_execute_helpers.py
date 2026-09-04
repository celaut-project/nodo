"""What is left in the CH backend once the shared machinery moved out.

Its process invocation, its API socket, and the cmdline it hands the guest. The
bundle checks, the guest injections, the id generation and the addressing token
these tests used to cover are the microVM family's -- see
``test_microvm_guest_preparation.py``.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.ch import execute as ch_execute
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_execute = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorProcessTests(unittest.TestCase):
    def test_build_ch_process_args_exposes_nodo_name_in_argv0(self):
        # argv[0] is what every later reader matches the PID against, so a
        # recycled PID cannot be mistaken for this VM.
        args = ch_execute._build_ch_process_args(
            start_command=[
                "/nodo/bin/cloud-hypervisor",
                "--api-socket",
                "/tmp/ch.sock",
            ],
            vmachine_id="f47b647a-eb0f-4518-8c8e-da40654bec4d",
        )
        self.assertEqual(args[0], "nodo-ch-f47b647a")
        self.assertEqual(args[1:], ["--api-socket", "/tmp/ch.sock"])

    def test_api_socket_path_uses_short_tmp_path_for_long_hash_ids(self):
        # An AF_UNIX path is capped at 108 bytes and a runtime directory keyed by
        # the full 64-hex id can exceed that on its own.
        vmachine_id = "a" * 64
        with patch(
            "src.virtualizers.microvm.paths.control_socket_dir",
            return_value=ch_execute.Path("/tmp/nodo-ch"),
        ):
            socket_path = ch_execute._api_socket_path(vmachine_id)

        self.assertEqual(str(socket_path), "/tmp/nodo-ch/ch-aaaaaaaaaaaaaaaa.sock")
        self.assertLess(len(str(socket_path)), 108)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KernelCmdlineTests(unittest.TestCase):
    """The cmdline that launches every service. An error here starts nothing."""

    def _cmdline(self, machine, extra):
        with patch.object(ch_execute.microvm_guest, "platform") as fake_platform:
            fake_platform.machine.return_value = machine
            with patch.object(ch_execute, "KERNEL_CMDLINE_EXTRA", extra):
                with patch.object(ch_execute.network, "GUEST_NET_DEVICE", "auto"):
                    return ch_execute._kernel_cmdline(vm_ip="192.168.200.5",
                                                      netmask="255.255.255.0")

    def test_arm_guests_are_told_about_the_pl011(self):
        self.assertIn("console=ttyAMA0", self._cmdline("aarch64", ""))

    def test_x86_guests_are_told_about_the_8250(self):
        self.assertIn("console=ttyS0", self._cmdline("x86_64", ""))

    def test_a_stale_console_in_the_config_is_dropped(self):
        # config.example.yaml shipped `console=ttyS0` and told operators to keep it,
        # so it is in the config of every node installed before this. Honouring it on
        # arm64 panics PID 1 before /init prints anything, so it cannot be honoured.
        cmdline = self._cmdline("aarch64", "console=ttyS0")

        self.assertIn("console=ttyAMA0", cmdline)
        self.assertNotIn("ttyS0", cmdline)

    def test_genuinely_extra_parameters_survive_alongside_the_console(self):
        cmdline = self._cmdline("aarch64", "console=ttyS0 loglevel=7 nokaslr")

        self.assertIn("console=ttyAMA0", cmdline)
        self.assertIn("loglevel=7", cmdline)
        self.assertIn("nokaslr", cmdline)
        self.assertNotIn("ttyS0", cmdline)

    def test_exactly_one_console_is_ever_passed(self):
        for machine, extra in (("aarch64", "console=ttyAMA0"), ("x86_64", "console=ttyS0"),
                               ("aarch64", ""), ("aarch64", "console=hvc0")):
            with self.subTest(machine=machine, extra=extra):
                cmdline = self._cmdline(machine, extra)
                self.assertEqual(cmdline.count("console="), 1)

    def test_the_root_device_and_ip_are_still_there(self):
        cmdline = self._cmdline("aarch64", "")

        self.assertIn("root=/dev/vda", cmdline)
        self.assertIn("rw", cmdline.split())
        self.assertIn("ip=192.168.200.5::", cmdline)


if __name__ == "__main__":
    unittest.main()
