"""`nodo info` names the node's identity key (issue #395).

Why this is worth a test at all: the hex `nodo info` prints here is not decoration
and not a second version string. It is the *same* string the reputation system keys
by (`node_id` in ``src/reputation_system/interface.py``), so it is what an operator
compares against when a peer says it vouched for them, and what they paste when
asking one to. Printed wrong -- or printed as an empty value because the identity
mnemonic is not there yet -- it sends somebody looking for a bug in the chain.

The block is read out of ``nodo.py`` and executed, rather than imported: importing
the dispatcher runs the whole node's import graph, and `nodo info` is a `match` arm
with no function to call. Executing the real source is what keeps this a test of the
code that ships and not of a copy of it. The same trick `tests/test_payment_method_selector.py`
uses on ``take_options``.
"""
import io
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock


def _identity_block() -> str:
    """The `Node id:` lines of `nodo info`, dedented into something executable.

    Delimited by the comment that opens the block and the `port = gateway_port()`
    that follows it, so a change to either end of the block is a failing test rather
    than a silently skipped one.
    """
    source = open("nodo.py", encoding="utf-8").read()
    start = source.index("                # The node's identity key, printed here")
    end = source.index("                port = gateway_port()")
    return textwrap.dedent(source[start:end])


def _run(node_id_result):
    """Execute the block with `get_node_public_key_hex` replaced, and capture stdout.

    `node_id_result` is either the hex the function returns or an exception it raises.
    """
    module = mock.Mock()
    if isinstance(node_id_result, Exception):
        module.get_node_public_key_hex.side_effect = node_id_result
    else:
        module.get_node_public_key_hex.return_value = node_id_result

    logged = []
    namespace = {"log": mock.Mock(LOGGER=logged.append)}
    with mock.patch.dict(
        "sys.modules", {"src.identity.node_identity": module}
    ), redirect_stdout(io.StringIO()) as out:
        exec(compile(_identity_block(), "nodo.py", "exec"), namespace)
    return out.getvalue(), logged


class InfoNodeIdTests(unittest.TestCase):
    def test_a_node_with_an_identity_prints_its_public_key(self):
        hex_key = "a" * 64
        printed, logged = _run(hex_key)

        self.assertEqual(printed.strip(), f"Node id: {hex_key}")
        self.assertEqual(logged, [])

    def test_a_node_with_no_mnemonic_yet_says_so_rather_than_printing_nothing(self):
        """None is an ordinary state, not a failure: say which one it is.

        A blank after `Node id:` reads like a bug in the identity code; "no identity
        mnemonic yet" tells the operator to start the node once and look again.
        """
        printed, logged = _run(None)

        self.assertIn("unavailable", printed)
        self.assertIn("no identity mnemonic yet", printed)
        self.assertEqual(logged, [])

    def test_identity_that_cannot_be_read_does_not_stop_the_rest_of_info(self):
        """Wrapped like every neighbouring block: `nodo info` prints what it can.

        Identity lives behind config that may be unreadable on a half-installed node,
        and the address, the DDNS status and the wallets underneath it are exactly
        what somebody diagnosing that install needs.
        """
        printed, logged = _run(RuntimeError("no config"))

        self.assertIn("Node id: unavailable", printed)
        self.assertIn("no config", printed)
        self.assertTrue(any("no config" in line for line in logged))


class InfoOrderTests(unittest.TestCase):
    def test_the_node_id_is_printed_directly_after_the_version(self):
        """Identity belongs with the other "which node is this" lines, not below the
        wallets: an operator scanning for it should not have to read past a JVM
        balance lookup that may be several seconds and several lines away."""
        source = open("nodo.py", encoding="utf-8").read()

        version = source.index('print(f"Nodo version: {get_git_commit()}"')
        node_id = source.index('f"Node id: {node_id}"')
        address = source.index('print(f"Nodo address:')

        self.assertLess(version, node_id)
        self.assertLess(node_id, address)


if __name__ == "__main__":
    unittest.main()
