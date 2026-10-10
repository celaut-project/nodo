"""``nodo resources --json``: this node's own announcement, as the TUI reads it (#455).

The JSON carries the serialized ``Peer`` so the TUI decodes it with the code it decodes
every peer's stored announcement with; this pins that the bytes are exactly
``announced_resources()`` and that a failure is still one JSON line.
"""
import base64
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.commands import resources as command  # noqa: E402
from src.utils.cost_functions import architecture_resources as ar  # noqa: E402

GIB = 1 << 30


def _entry(tags, cores, mem):
    entry = celaut.ArchitectureResources()
    entry.architecture.tags.extend(tags)
    entry.resources.cpu_period = 100_000
    entry.resources.cpu_quota = cores * 100_000
    entry.resources.mem_limit = mem
    return entry


def _run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        ok = command.resources(argv=argv)
    return ok, out.getvalue()


class ResourcesCommandTests(unittest.TestCase):

    def test_json_is_the_announced_peer_resources(self):
        entries = [_entry(["linux/amd64", "x86_64"], 8, 16 * GIB), _entry(["linux/arm64"], 4, 8 * GIB)]
        with patch.object(ar, "announced_resources", return_value=entries):
            ok, out = _run(["--json"])
        self.assertTrue(ok)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        report = json.loads(lines[0])
        peer = celaut.Peer()
        peer.ParseFromString(base64.b64decode(report["peer"]))
        self.assertEqual(list(peer.resources), entries)
        # Nothing but resources: this is not an announcement anyone could verify.
        self.assertEqual([field.name for field, _ in peer.ListFields()], ["resources"])

    def test_nothing_announced_is_an_empty_peer(self):
        with patch.object(ar, "announced_resources", return_value=[]):
            ok, out = _run(["--json"])
        self.assertTrue(ok)
        self.assertEqual(base64.b64decode(json.loads(out)["peer"]), b"")

    def test_the_printed_form_says_a_delegating_node_announces_nothing_on_purpose(self):
        with patch.object(ar, "announced_resources", return_value=[]), \
                patch.object(ar, "executes_locally", return_value=False):
            ok, out = _run([])
        self.assertTrue(ok)
        self.assertIn("delegates only", out)

    def test_a_failure_is_still_one_json_line(self):
        with patch.object(ar, "announced_resources", side_effect=RuntimeError("no psutil")):
            ok, out = _run(["--json"])
        self.assertFalse(ok)
        self.assertEqual(json.loads(out)["error"], "no psutil")

    def test_the_printed_form_names_every_architecture(self):
        entries = [_entry(["linux/amd64"], 8, 16 * GIB)]
        entries[0].resources.benchmark.add(key="int_ops_per_sec", value=1000)
        with patch.object(ar, "announced_resources", return_value=entries):
            ok, out = _run([])
        self.assertTrue(ok)
        self.assertIn("linux/amd64", out)
        self.assertIn("cores   8", out)
        self.assertIn("disk    not stated", out)
        self.assertIn("int_ops_per_sec", out)


if __name__ == "__main__":
    unittest.main()
