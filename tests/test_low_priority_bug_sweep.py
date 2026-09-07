"""Four small failures, each in its own place, none of which had a case pinning it.

Grouped because they were reported and fixed together, not because they are related:

- a hostile certificate must reach a caller as `CertificateError` and nothing else (#312)
- a peer that announced no owner attestation has claimed nothing to disbelieve (#315)
- one arch resolution, so the overhead the operator is shown is the one the VM boots
  with (#321)
- a panic marker only counts at the start of a real line, including at the seam of the
  tail window (#328)
"""
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from cryptography import x509
    from protos import celaut_pb2
    from src.identity.tls_identity import CertificateError, peer_id_from_certificate
    from src.manager import manager
    from src.virtualizers.microvm import limits
    from src.virtualizers.microvm.guest_panic import (
        SERIAL_TAIL_BYTES,
        guest_panic_line,
        read_serial_tail,
    )
    from src.utils.contract_xattrs import set_owner_attestation
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PANIC = "Kernel panic - not syncing: Attempted to kill init!"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HostileCertificateTests(unittest.TestCase):
    """A certificate comes from whatever an unknown address answered with (issue #312).

    Callers handle a refusal by catching `CertificateError` -- the payment path's
    `except (ConnectionError, CertificateError)` among them -- so anything else escaping
    from here turns "cannot reach this peer" into a crash on the way to paying somebody.
    """

    def test_an_unparseable_certificate_is_a_certificate_error(self):
        with self.assertRaises(CertificateError):
            peer_id_from_certificate(b"not a certificate")

    def test_a_duplicate_extension_is_a_certificate_error(self):
        # x509.DuplicateExtension is no subclass of CertificateError. Driven through a
        # stub because `CertificateBuilder` refuses to emit such a certificate, while
        # `load_der_x509_certificate` parses one built by hand quite happily.
        class Extensions:
            def get_extension_for_oid(self, oid):
                raise x509.DuplicateExtension("twice", oid)

        certificate = unittest.mock.Mock(extensions=Extensions())
        with unittest.mock.patch(
            "src.identity.tls_identity.x509.load_der_x509_certificate",
            return_value=certificate,
        ):
            with self.assertRaises(CertificateError):
                peer_id_from_certificate(b"whatever")

    def test_any_other_lookup_failure_is_a_certificate_error(self):
        class Extensions:
            def get_extension_for_oid(self, oid):
                raise ValueError("malformed extension set")

        certificate = unittest.mock.Mock(extensions=Extensions())
        with unittest.mock.patch(
            "src.identity.tls_identity.x509.load_der_x509_certificate",
            return_value=certificate,
        ):
            with self.assertRaises(CertificateError):
                peer_id_from_certificate(b"whatever")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UnattestedProofIsNotADisownedProofTests(unittest.TestCase):
    """What the log says about a proof that fails the ownership check (issue #315).

    A proof arriving with no attestation says nothing about who holds it, so there is
    nothing to disbelieve; one carrying an attestation that does not verify is a claim
    this node checked and rejected. Both used to read as "does not own", which blamed a
    peer for having stated nothing.
    """

    def _lines(self, contract):
        peer = celaut_pb2.Peer(public_key="ab" * 32)
        lines = []
        with unittest.mock.patch.object(manager.log, "LOGGER", lines.append):
            verdict = manager.validate_reputation_proof(contract_ledger=contract, peer=peer)
        return verdict, " ".join(lines)

    def test_a_proof_with_no_attestation_is_not_accused_of_being_disowned(self):
        contract = celaut_pb2.Contract()
        verdict, logged = self._lines(contract)
        self.assertFalse(verdict)
        self.assertIn("no owner attestation", logged)
        self.assertNotIn("does not own", logged)

    def test_an_unusable_peer_id_is_not_reported_as_a_bad_attestation(self):
        # A third case: nothing is wrong with the proof, there is simply no key to check
        # its attestation against. Reachable only if an unverified peer got this far,
        # and reporting it as a bad attestation would be the same misattribution the
        # rest of this fix undoes.
        contract = celaut_pb2.Contract()
        set_owner_attestation(contract, "02" + "11" * 32, "ff" * 65)
        peer = celaut_pb2.Peer(public_key="not-a-key")
        lines = []
        with unittest.mock.patch.object(manager.log, "LOGGER", lines.append):
            manager.validate_reputation_proof(contract_ledger=contract, peer=peer)
        logged = " ".join(lines)
        self.assertIn("not a usable key", logged)
        self.assertNotIn("does not verify", logged)

    def test_an_attestation_that_does_not_verify_is_reported_as_such(self):
        contract = celaut_pb2.Contract()
        set_owner_attestation(contract, "02" + "11" * 32, "ff" * 65)
        verdict, logged = self._lines(contract)
        self.assertFalse(verdict)
        self.assertIn("does not verify", logged)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OneArchResolutionTests(unittest.TestCase):
    """The overhead shown to the operator is the one the VM boots with (issue #321).

    `guest_kernel_reserve_bytes` is what the TUI's pricing page displays;
    `guest_boot_memory_bytes` is what the launcher sizes the VM by. With no arch named
    they resolved it two different ways -- the largest measured reserve versus the
    host's -- so on any host that is not the one that fallback came from, the operator
    priced one figure and the guest booted with another.
    """

    USABLE = 512 * 1024 * 1024

    def _agree(self, host_arch):
        with unittest.mock.patch.object(limits, "host_arch_tag", lambda: host_arch):
            reserve = limits.guest_kernel_reserve_bytes(self.USABLE)
            boot = limits.guest_boot_memory_bytes(self.USABLE) - self.USABLE
        return reserve, boot

    def test_the_two_entry_points_agree_on_every_host(self):
        for host_arch in ("linux/amd64", "linux/arm64"):
            with self.subTest(host=host_arch):
                reserve, boot = self._agree(host_arch)
                self.assertEqual(reserve, boot)

    def test_an_absent_arch_resolves_to_the_host(self):
        with unittest.mock.patch.object(limits, "host_arch_tag", lambda: "linux/arm64"):
            self.assertEqual(
                limits._reserve_for_arch(None), limits._reserve_for_arch("linux/arm64")
            )

    def test_a_named_but_unknown_arch_keeps_the_conservative_fallback(self):
        # The largest measured reserve is still right for a guest nobody measured:
        # over-reserving is the safe direction there.
        with unittest.mock.patch.object(limits, "host_arch_tag", lambda: "linux/arm64"):
            fixed, _ = limits._reserve_for_arch("linux/riscv64")
        self.assertEqual(fixed, limits._FALLBACK_GUEST_KERNEL_RESERVE[0] * 1024 * 1024)

    def test_an_operator_override_can_be_found_without_naming_an_arch(self):
        # The prefix used to be built from `None`, so no override could ever match.
        with unittest.mock.patch.object(limits, "host_arch_tag", lambda: "linux/amd64"), \
             unittest.mock.patch.object(limits, "_env_int", lambda key, default: 7 if "linux/amd64" in key else default):
            fixed, _ = limits._reserve_for_arch(None)
        self.assertEqual(fixed, 7 * 1024 * 1024)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SerialTailStartsOnALineTests(unittest.TestCase):
    """The marker counts only at the start of a line the guest actually started (#328).

    `re.MULTILINE` makes `^` match at position 0, so a window that opens mid-line hands
    the matcher a fragment whose own prefix has been cut off. A guest printing
    `... Kernel panic - not syncing: x` as ordinary output, split at exactly the window
    boundary, would then be reaped and scored -100.
    """

    def _log(self, text):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        Path(path).write_text(text, encoding="utf-8")
        self.addCleanup(os.unlink, path)
        return path

    def test_a_line_split_by_the_window_does_not_match(self):
        # The window always opens exactly SERIAL_TAIL_BYTES from the end, so the marker
        # is placed that far back: the read then begins on `Kernel`, with the
        # `service says: ` that made it ordinary output left outside. Reading as a line
        # of its own is precisely what must not happen.
        tail_after = "z" * (SERIAL_TAIL_BYTES - len(PANIC) - 1)
        path = self._log("x" * 4096 + f"service says: {PANIC}\n" + tail_after)
        self.assertTrue(
            read_serial_tail(path) == "" or not read_serial_tail(path).startswith("Kernel"),
            "the window still opens on the marker",
        )
        self.assertIsNone(guest_panic_line({"serial_log": path}))

    def test_a_real_panic_at_the_start_of_a_line_still_matches(self):
        path = self._log("x" * (SERIAL_TAIL_BYTES // 2) + f"\n{PANIC}\n")
        self.assertEqual(guest_panic_line({"serial_log": path}), PANIC)

    def test_a_panic_behind_a_printk_timestamp_still_matches(self):
        path = self._log(f"[   12.345678] {PANIC}\n")
        self.assertEqual(guest_panic_line({"serial_log": path}), PANIC)

    def test_a_log_exactly_the_window_size_keeps_its_first_line(self):
        # The boundary the fix itself can get wrong: seeking -size from the end of a
        # file of exactly that size succeeds and lands on offset 0, so there is no
        # partial line -- dropping one here would hide a panic printed at the very top
        # of a log that then filled to the window size.
        body = f"{PANIC}\n"
        path = self._log(body + "z" * (SERIAL_TAIL_BYTES - len(body)))
        self.assertEqual(os.path.getsize(path), SERIAL_TAIL_BYTES)
        self.assertEqual(guest_panic_line({"serial_log": path}), PANIC)

    def test_a_short_log_keeps_its_first_line(self):
        # Nothing was cut, so there is no partial line to drop -- and dropping one would
        # hide a panic from a guest that printed little else.
        path = self._log(f"{PANIC}\n")
        self.assertEqual(guest_panic_line({"serial_log": path}), PANIC)

    def test_a_window_holding_one_unterminated_line_yields_nothing(self):
        # No newline anywhere in the window: there is no way to tell where the line
        # began, so nothing in it can be trusted to be at the start of one.
        path = self._log("y" * SERIAL_TAIL_BYTES + PANIC)
        self.assertEqual(read_serial_tail(path), "")

    def test_a_missing_log_reads_as_cannot_tell(self):
        self.assertEqual(read_serial_tail("/nonexistent/serial.log"), "")
        self.assertIsNone(guest_panic_line({"serial_log": "/nonexistent/serial.log"}))


if __name__ == "__main__":
    unittest.main()
