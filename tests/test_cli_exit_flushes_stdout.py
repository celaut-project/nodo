"""A command's output must survive ``os._exit`` when stdout is not a terminal (#489).

The incident behind this file: ``nodo services``, ``instances``, ``clients``, ``peers``
and ``status`` returned zero bytes and exit 0 through a pipe. A script, and an AI agent,
read that as "nothing there" and said so to a user. ``nodo.py`` ends a command with
``os._exit``, which skips the flush of a block-buffered stdout.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

import nodo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Stream(StringIO):
    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail
        self.flushed = False

    def flush(self):
        if self.fail:
            raise BrokenPipeError("closed")
        self.flushed = True


class ExitHelperTests(unittest.TestCase):
    def test_flushes_both_streams_before_exiting(self):
        out, err = _Stream(), _Stream()
        order = []
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            with patch.object(nodo.os, "_exit", side_effect=lambda c: order.append((c, out.flushed, err.flushed))):
                nodo._exit(3)

        self.assertEqual(order, [(3, True, True)])

    def test_a_closed_pipe_still_exits_with_the_code(self):
        codes = []
        with patch.object(sys, "stdout", _Stream(fail=True)), patch.object(sys, "stderr", _Stream(fail=True)):
            with patch.object(nodo.os, "_exit", side_effect=codes.append):
                nodo._exit(1)

        self.assertEqual(codes, [1])

    def test_nothing_in_the_dispatcher_bypasses_the_helper(self):
        with open(os.path.join(REPO, "nodo.py")) as f:
            bare = [line.strip() for line in f if "os._exit(" in line]

        # Only the helper's own last line may call it.
        self.assertEqual(bare, ["os._exit(code)"])


class RedirectedOutputTests(unittest.TestCase):
    def test_printed_text_reaches_a_file_through_the_helper(self):
        # The real failure: block-buffered stdout, then os._exit.
        script = "import nodo\nprint('services listed')\nnodo._exit(0)\n"
        with tempfile.TemporaryFile() as out:
            proc = subprocess.run(
                [sys.executable, "-c", script], cwd=REPO, stdout=out,
                stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": ""},
            )
            out.seek(0)
            written = out.read().decode()

        self.assertEqual(proc.returncode, 0)
        self.assertIn("services listed", written)

    def test_the_bare_os_exit_loses_it(self):
        # Pins the premise: without the flush the text is dropped, so the test above
        # would fail if the helper stopped flushing.
        script = "import os\nprint('services listed')\nos._exit(0)\n"
        with tempfile.TemporaryFile() as out:
            subprocess.run(
                [sys.executable, "-c", script], stdout=out, stderr=subprocess.DEVNULL,
                env={k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"},
            )
            out.seek(0)
            self.assertEqual(out.read(), b"")


if __name__ == "__main__":
    unittest.main()
