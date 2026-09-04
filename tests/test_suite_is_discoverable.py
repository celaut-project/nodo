"""Every test file in this tree is actually run (issue #308).

`unittest discover` walks packages, so a directory holding tests but no `__init__.py`
is skipped in silence -- and a skipped directory is indistinguishable from a passing
one. `tests/payment_system/` sat like that from the day it was created: thirteen tests
never ran, and three of them had been calling a function whose signature changed three
days later, with nothing to say so.

This is the check that makes the next one impossible rather than merely fixed. It
belongs at the top of the tree because it is about the tree.
"""
import os
import unittest

TESTS_ROOT = os.path.dirname(os.path.abspath(__file__))

# Never holds tests, and is not ours to put a file in.
_IGNORED = {"__pycache__"}


def _directories_holding_tests():
    """Every directory under ``tests/`` with at least one ``test_*.py`` in it."""
    for root, dirs, names in os.walk(TESTS_ROOT):
        dirs[:] = [d for d in dirs if d not in _IGNORED]
        if root == TESTS_ROOT:
            continue
        if any(n.startswith("test_") and n.endswith(".py") for n in names):
            yield root


class SuiteIsDiscoverableTests(unittest.TestCase):
    def test_every_directory_with_tests_is_a_package(self):
        missing = [
            os.path.relpath(d, os.path.dirname(TESTS_ROOT))
            for d in _directories_holding_tests()
            if not os.path.isfile(os.path.join(d, "__init__.py"))
        ]
        self.assertEqual(
            missing,
            [],
            "these directories hold tests that `unittest discover` will not find; "
            "add an empty __init__.py to each: " + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
