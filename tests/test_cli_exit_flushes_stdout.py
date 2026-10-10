"""A command's output must survive ``os._exit`` when stdout is not a terminal (#489).

The incident behind this file: ``nodo services``, ``instances``, ``clients``, ``peers``
and ``status`` returned zero bytes and exit 0 through a pipe. A script, and an AI agent,
read that as "nothing there" and said so to a user. ``nodo.py`` ends a command with
``os._exit``, which skips the flush of a block-buffered stdout.

The helper is read out of ``nodo.py`` and executed, rather than imported: importing
the dispatcher runs the whole node's import graph and loads its config. The same
trick ``tests/test_info_node_id.py`` uses.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

_NODO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nodo.py")


def _helper_source() -> str:
    """``def _exit`` in ``nodo.py``, up to the next top-level definition."""
    source = open(_NODO, encoding="utf-8").read()
    start = source.index("def _exit(")
    end = source.index("\ndef ", start + 1)
    return "import os, sys\n" + source[start:end]


def _helper():
    namespace = {}
    exec(compile(_helper_source(), "nodo.py", "exec"), namespace)
    return namespace["_exit"], namespace["os"]


def _buffered_env():
    """The caller's environment, minus anything that would unbuffer stdout."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}


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
        exit_, os_module = _helper()
        out, err = _Stream(), _Stream()
        seen = []
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            with patch.object(os_module, "_exit", side_effect=lambda code: seen.append(
                (code, out.flushed, err.flushed)
            )):
                exit_(3)

        self.assertEqual(seen, [(3, True, True)])

    def test_a_closed_pipe_still_exits_with_the_code(self):
        exit_, os_module = _helper()
        codes = []
        with patch.object(sys, "stdout", _Stream(fail=True)), \
                patch.object(sys, "stderr", _Stream(fail=True)):
            with patch.object(os_module, "_exit", side_effect=codes.append):
                exit_(1)

        self.assertEqual(codes, [1])

    def test_nothing_in_the_dispatcher_bypasses_the_helper(self):
        with open(_NODO, encoding="utf-8") as f:
            bare = [line.strip() for line in f if "os._exit(" in line]

        # Only the helper's own last line may call it.
        self.assertEqual(bare, ["os._exit(code)"])


class RedirectedOutputTests(unittest.TestCase):
    def _run_to_file(self, script):
        with tempfile.TemporaryFile() as out:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                stdout=out, stderr=subprocess.DEVNULL, env=_buffered_env(),
            )
            out.seek(0)
            return proc.returncode, out.read().decode()

    def test_printed_text_reaches_a_file_through_the_helper(self):
        # The real failure: block-buffered stdout, then os._exit.
        code, written = self._run_to_file(
            _helper_source() + "\nprint('services listed')\n_exit(0)\n"
        )

        self.assertEqual(code, 0)
        self.assertEqual(written, "services listed\n")

    def test_the_bare_os_exit_loses_it(self):
        # Pins the premise: without the flush the text is dropped, so the test above
        # would fail if the helper stopped flushing.
        code, written = self._run_to_file("import os\nprint('services listed')\nos._exit(0)\n")

        self.assertEqual(code, 0)
        self.assertEqual(written, "")


if __name__ == "__main__":
    unittest.main()
