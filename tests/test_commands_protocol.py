"""``nodo protocol``: what this node declares, and how a peer's declaration compares.

The command reads no wire of its own: the declaration is built by the same functions
the announcement is, and the comparison is the node's own (tags and formal, never
prose). These pin that it stays so -- that the document it prints is what peers
receive, and that its verdict is the one ``add_peer_instance`` would reach.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.commands import protocol
    from src.identity import transport_stack
    from src.identity.node_identity import declare_signature_scheme
    from src.manager import manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _peer(*, stack=True):
    peer = celaut_pb2.Peer(public_key="ab" * 32)
    uri = peer.uri.add(ip="1.2.3.4", port=8080)
    transport_stack.declare_transport(uri, prose=False)
    if stack:
        transport_stack.declare_transport_stack(uri, prose=False)
    declare_signature_scheme(peer, prose=False)
    return peer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OwnDeclarationTests(unittest.TestCase):
    def test_it_is_the_stack_peers_receive(self):
        declared = protocol.own_declaration(prose=False)["protocol_stack"]
        announced = transport_stack.node_transport_stack(prose=False)
        self.assertEqual(
            [(d["tags"], d["formal"].encode()) for d in declared],
            [(list(c.tags), bytes(c.formal)) for c in announced],
        )

    def test_the_prose_is_there_even_though_announcements_drop_it(self):
        # GetPeerInfo leaves the prose out by default; this is where an operator or an
        # implementer reads it anyway.
        declaration = protocol.own_declaration()
        self.assertTrue(all(c["prose"] for c in declaration["protocol_stack"]))
        self.assertTrue(all(c["prose"] for c in declaration["signature_scheme"]))

    def test_no_prose_leaves_it_out(self):
        declaration = protocol.own_declaration(prose=False)
        self.assertTrue(all("prose" not in c for c in declaration["protocol_stack"]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ComparisonTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            manager, "verified_peer_public_key", side_effect=lambda p: p.public_key
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_peer_speaking_our_stack_is_compatible(self):
        report = protocol.compare_with(_peer(), "ab" * 32)
        self.assertTrue(report["compatible"])
        self.assertEqual(
            [l["status"] for l in report["uris"][0]["layers"]], ["match"] * 5
        )

    def test_a_different_layer_makes_it_incompatible_and_says_where(self):
        peer = _peer()
        tls = next(c for c in peer.uri[0].protocol_stack if "tls" in c.tags)
        tls.formal = tls.formal.replace(b"min_version=1.3", b"min_version=1.2")
        report = protocol.compare_with(peer, "ab" * 32)
        self.assertFalse(report["compatible"])
        layer = next(l for l in report["uris"][0]["layers"] if "tls" in l["tags"])
        self.assertEqual(list(layer["formal_difference"]), ["min_version"])

    def test_an_undeclared_stack_is_not_compatible(self):
        # The same reading add_peer_instance makes: nothing declared, nothing spoken.
        report = protocol.compare_with(_peer(stack=False), "ab" * 32)
        self.assertFalse(report["compatible"])
        self.assertEqual({l["status"] for l in report["uris"][0]["layers"]}, {"missing"})

    def test_an_undeclared_signature_scheme_is_not_compatible(self):
        peer = _peer()
        peer.ClearField("signature_scheme")
        self.assertFalse(protocol.compare_with(peer, "ab" * 32)["compatible"])

    def test_a_udp_only_peer_is_not_compatible(self):
        peer = _peer()
        peer.uri[0].transport.CopyFrom(celaut_pb2.Peer.Uri.Protocol(tags=["udp"]))
        self.assertFalse(protocol.compare_with(peer, "ab" * 32)["compatible"])

    def test_a_signature_that_does_not_verify_is_not_compatible(self):
        # add_peer_instance would refuse this peer, whatever it speaks (#467).
        with mock.patch.object(manager, "verified_peer_public_key", return_value=None):
            report = protocol.compare_with(_peer(), "ab" * 32)
        self.assertFalse(report["signature_verifies"])
        self.assertTrue(report["uris"][0]["speaks"])
        self.assertFalse(report["compatible"])

    def test_an_address_held_by_another_identity_is_not_compatible(self):
        report = protocol.compare_with(_peer(), "cd" * 32)
        self.assertFalse(report["identity_matches"])
        self.assertFalse(report["compatible"])

    def test_an_unknown_holder_is_not_compatible(self):
        report = protocol.compare_with(_peer(), None)
        self.assertFalse(report["identity_matches"])
        self.assertFalse(report["compatible"])

    def test_the_help_says_an_address_gets_a_client(self):
        from src.commands import help as help_command

        line = next(l for l in help_command.render_help().splitlines() if "protocol [<peer>]" in l)
        self.assertIn("client", line)
        self.assertLess(len(line), 80)


if __name__ == "__main__":
    unittest.main()
