"""``nodo receipt verify`` on files that the client did not write (#419, #593 review).

A malformed file gives "Cannot read receipts" and exit False, as text or as one JSON
error with ``--json``. Never a traceback.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()   # src.commands.execute reads the storage path of the config
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from protos import celaut_pb2
    from src.commands import receipt as receipt_command
    from src.identity import execution_receipt as er
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VerifyCommandTests(unittest.TestCase):

    BAD = {
        "an entry that is not an object": "[1]",
        "a list of lists": "[[]]",
        "a number": "5",
        "a receipts key that is not a list": '{"receipts": 5}',
        "an unknown field": '{"kind": "start", "no_such_field": 1}',
        "a bad base64 value": '{"envs_salt": "a"}',
        "a number where bytes go": '{"envs_salt": 5}',
        "a wrong type": '{"seq": "many"}',
        "a message where a text goes": '{"kind": {"a": 1}}',
        "not JSON": "{not json",
        "a JSON nested too deep": "[" * 100000 + "]" * 100000,
        "bytes that are not UTF-8": b"\xff\xfe\x00",
    }

    def _file(self, content):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode())
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = receipt_command.receipt_command(argv)
        return ok, out.getvalue()

    def test_a_malformed_file_is_reported_not_raised(self):
        for name, content in self.BAD.items():
            path = self._file(content)
            with self.subTest(case=name, shape="text"):
                ok, out = self._run(["verify", path])
                self.assertFalse(ok)
                self.assertIn("Cannot read receipts", out)
            with self.subTest(case=name, shape="json"):
                ok, out = self._run(["verify", path, "--json"])
                self.assertFalse(ok)
                self.assertIn("Cannot read receipts", json.loads(out)["error"])

    def test_a_missing_file(self):
        ok, out = self._run(["verify", "/nonexistent/receipts.json", "--json"])
        self.assertFalse(ok)
        self.assertIn("Cannot read receipts", json.loads(out)["error"])

    def test_a_good_file_still_verifies(self):
        key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        receipt = er.start_receipt(
            "tok", celaut_pb2.Metadata.HashTag.Hash(type=b"t", value=b"s"), {}, 1,
            signer=lambda r: er.sign_with(r, key),
            peer_id="03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
        path = self._file("")
        receipt_command.write_receipt(path, receipt)
        ok, out = self._run(["verify", path, "--token", "tok", "--json"])
        self.assertTrue(ok, out)
        self.assertTrue(json.loads(out)["valid"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SaveStartReceiptTests(unittest.TestCase):
    """Final review of #593: ``nodo execute --receipt`` writes the file after the start.
    A path that cannot be written raised OSError, after the start was paid, and the
    receipt was lost: the node does not keep it."""

    def test_a_path_that_cannot_be_written_prints_the_receipt(self):
        from src.commands.execute import save_start_receipt
        key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        receipt = er.start_receipt(
            "tok", celaut_pb2.Metadata.HashTag.Hash(type=b"t", value=b"s"), {}, 1,
            signer=lambda r: er.sign_with(r, key),
            peer_id="03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
        response = celaut_pb2.ServiceInstance(token="tok", receipt=receipt)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            save_start_receipt(response, "/nonexistent/dir/receipt.json")
        lines = out.getvalue().strip().splitlines()
        self.assertIn("Cannot write the start receipt", lines[0])
        self.assertEqual(er.from_json_dict(json.loads(lines[-1])), receipt)


if __name__ == "__main__":
    unittest.main()
