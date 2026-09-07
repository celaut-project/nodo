"""``nodo help`` must describe exactly the commands nodo dispatches.

A help screen that lists a command nodo removed, or omits one it gained, is worse
than no help screen: the operator trusts it. So the catalogue in
``src/commands/help.py`` is checked against the two other places the command set
is written down -- the ``match sys.argv[1]`` dispatch in ``nodo.py`` and
``COMMANDS`` in ``src/commands/completion.py``.
"""

import os
import re
import unittest

from src.commands import completion
from src.commands import help as help_command

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `case "execute":`, `case 'clients':`, `case "help" | "-h" | "--help":` …
_CASE = re.compile(r"^\s+case\s+(.+?):\s*$", re.MULTILINE)
_LITERAL = re.compile(r"""["']([\w:.\-]+)["']""")

# Spellings of a documented command, not commands of their own.
ALIASES = {"-h", "--help"}

# The terminal the help is read in. Every line has to fit without wrapping.
TERMINAL_WIDTH = 80


def dispatched_commands():
    """The command names ``nodo.py`` matches on, aliases and ``case other`` aside."""
    with open(os.path.join(_ROOT, "nodo.py")) as f:
        source = f.read()
    dispatched = {
        name
        for case in _CASE.findall(source)
        for name in _LITERAL.findall(case)
    }
    return dispatched - ALIASES


class HelpCatalogueTests(unittest.TestCase):
    def test_documents_every_dispatched_command(self):
        documented = set(help_command.commands())
        dispatched = dispatched_commands()
        self.assertTrue(dispatched, "the case parser found nothing -- fix the regex")
        self.assertEqual(dispatched - documented, set(), "dispatched but undocumented")
        self.assertEqual(documented - dispatched, set(), "documented but not dispatched")

    def test_agrees_with_the_completion_catalogue(self):
        self.assertEqual(set(help_command.commands()), set(completion.COMMANDS))

    def test_each_command_is_documented_once(self):
        commands = help_command.commands()
        self.assertEqual(len(commands), len(set(commands)))

    def test_every_entry_has_a_description(self):
        for usage, description in help_command.entries():
            self.assertTrue(description.strip(), usage)

    def test_lines_fit_a_terminal(self):
        for text in (help_command.render_help(), help_command.render_quick_start()):
            for line in text.splitlines():
                self.assertLess(len(line), TERMINAL_WIDTH, line)

    def test_quick_start_is_a_subset_of_the_catalogue(self):
        self.assertLessEqual(
            set(help_command.QUICK_START), set(help_command.commands())
        )

    def test_groups_are_ordered_as_written(self):
        # The grouping is the whole point: the rendered text keeps the catalogue's
        # order, so `help` never turns back into an alphabetical wall.
        rendered = help_command.render_help()
        positions = [rendered.index(f"\n{title}") for title, _, _ in help_command.GROUPS]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
