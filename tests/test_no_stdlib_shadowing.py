"""No module in this tree may share a name with a standard-library module.

Running any script puts its own directory first on ``sys.path``, so a module
sitting beside it wins over the stdlib module of the same name -- not only for
what that script imports, but for what the stdlib imports internally.

This is not hypothetical. ``src/commands/inspect.py`` shadowed ``inspect`` for
``src/commands/completion.py``, the helper the shell runs on every Tab keypress.
The failure surfaced much later and nowhere near the cause: adding a
``dataclasses`` import to an unrelated module broke service-name completion,
because ``dataclasses`` imports ``inspect`` and got the nodo command instead --
and completion swallows exceptions, so it just silently stopped offering names.

A one-line test is worth more than the comment explaining the last occurrence.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories whose modules can end up first on sys.path, i.e. anywhere a script
# may be executed from. In practice that is the whole package.
_SEARCH_ROOTS = ("src", "protos", "tests")

# Names that are ours and predate this rule, kept deliberately.
_ALLOWED = frozenset()


def _shadowing_modules():
    stdlib = set(sys.stdlib_module_names)
    found = []
    for root in _SEARCH_ROOTS:
        base = os.path.join(_ROOT, root)
        for directory, _subdirs, files in os.walk(base):
            if "__pycache__" in directory:
                continue
            for name in files:
                if not name.endswith(".py") or name == "__init__.py":
                    continue
                stem = name[:-3]
                if stem in stdlib and stem not in _ALLOWED:
                    found.append(os.path.relpath(os.path.join(directory, name), _ROOT))
    return sorted(found)


class StdlibShadowingTests(unittest.TestCase):
    def test_no_module_shadows_the_standard_library(self):
        shadowing = _shadowing_modules()
        self.assertEqual(
            shadowing,
            [],
            "These modules shadow a standard-library module of the same name and will "
            "break any script run from their directory (and anything the stdlib imports "
            f"internally): {shadowing}. Rename them, e.g. inspect.py -> inspect_service.py.",
        )

    def test_the_check_would_catch_a_regression(self):
        # Guard against the test quietly becoming a no-op if sys.stdlib_module_names
        # or the search roots ever stop resolving.
        self.assertIn("inspect", sys.stdlib_module_names)
        self.assertTrue(os.path.isdir(os.path.join(_ROOT, "src", "commands")))


if __name__ == "__main__":
    unittest.main()
