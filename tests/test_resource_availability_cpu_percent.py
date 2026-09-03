"""Issue #288: ``get_resource_availability`` must not sleep on ``cpu_percent``.

Parsed from source so the assertion does not depend on bee_rpc / ConfigManager.
The sample is write-only, but it still has to be cheap: this function runs on
every local launch and also answers peers over GetResourceAvailability.
"""
import ast
import unittest
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "utils"
    / "cost_functions"
    / "resource_availability.py"
)


class GetResourceAvailabilityCpuPercentTests(unittest.TestCase):
    def test_cpu_percent_uses_nonblocking_interval(self):
        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        intervals = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "cpu_percent"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            self.assertIn("interval", kwargs, "cpu_percent must pass interval explicitly")
            interval = kwargs["interval"]
            self.assertIsInstance(interval, ast.Constant)
            intervals.append(interval.value)
        self.assertEqual(intervals, [None])
