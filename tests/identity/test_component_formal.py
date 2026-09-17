"""``component_formal`` and its inverse: the body every celaut ``formal`` is written in.

The field is ``bytes`` in the proto and a ``key=value`` document by convention
(``Peer.SignatureScheme`` in celaut.proto). A signature scheme builds one here; a
``pow:<chain>`` network's arrives from a service spec somebody typed (issue #78). So
the parser is handed hand-written input, and what it refuses matters as much as what
it reads: a constraint dropped silently is a constraint nobody applied.

Nothing is stubbed. ``node_identity`` needs no wallet derivation to be imported, and
stubbing ``bip32`` here -- as the neighbouring modules do for their own reasons -- would
leak the stub into the collection of every test file sorted after this one, letting
``test_node_identity.py`` import against a fake instead of skipping.
"""
import unittest

from src.identity import node_identity as ni  # noqa: E402


class RoundTripTests(unittest.TestCase):
    def test_what_component_formal_writes_is_what_parse_reads_back(self):
        pairs = {"curve": "edwards25519", "algorithm": "eddsa", "prehash": "none"}

        self.assertEqual(ni.parse_component_formal(ni.component_formal(pairs)), pairs)

    def test_the_bytes_do_not_depend_on_the_order_the_pairs_were_built_in(self):
        """It is compared byte for byte and covered by a signature, so it must not."""
        forwards = ni.component_formal({"a": "1", "b": "2", "c": "3"})
        backwards = ni.component_formal({"c": "3", "b": "2", "a": "1"})

        self.assertEqual(forwards, backwards)

    def test_an_empty_formal_is_no_pairs_rather_than_an_error(self):
        """Empty is the "nothing determinate to point at" default, not a malformed one."""
        self.assertEqual(ni.parse_component_formal(b""), {})
        self.assertEqual(ni.parse_component_formal(b"   \n  "), {})

    def test_a_value_may_contain_an_equals_sign_and_a_key_may_not(self):
        """Splitting on the first `=` is what keeps the encoding unambiguous."""
        parsed = ni.parse_component_formal(b"spec=RFC8032 section=5.1.7&x=1")

        self.assertEqual(parsed, {"spec": "RFC8032 section=5.1.7&x=1"})

    def test_a_hand_written_document_may_end_in_a_newline(self):
        self.assertEqual(ni.parse_component_formal(b"curve=edwards25519\n"),
                         {"curve": "edwards25519"})

    def test_a_blank_line_inside_the_document_is_refused(self):
        """Whitespace around the document is ignored; nothing inside it is."""
        with self.assertRaises(ni.ComponentFormalError):
            ni.parse_component_formal(b"a=1\n\nb=2")

    def test_a_line_that_is_not_a_pair_is_refused_rather_than_skipped(self):
        with self.assertRaises(ni.ComponentFormalError) as raised:
            ni.parse_component_formal(b"a=1\nprose goes here\nb=2")

        self.assertIn("line 2", str(raised.exception))

    def test_an_empty_key_is_refused(self):
        with self.assertRaises(ni.ComponentFormalError):
            ni.parse_component_formal(b"=1")

    def test_a_repeated_key_is_refused_rather_than_resolved(self):
        """Which of the two was meant is not something a parser gets to decide."""
        with self.assertRaises(ni.ComponentFormalError) as raised:
            ni.parse_component_formal(b"chain=ergo\nchain=bitcoin")

        self.assertIn("twice", str(raised.exception))

    def test_bytes_that_are_not_utf8_are_refused(self):
        with self.assertRaises(ni.ComponentFormalError):
            ni.parse_component_formal(b"\xff\xfe")

    def test_the_declared_signature_scheme_parses_as_one_of_these_documents(self):
        """The convention this node writes and the convention it reads are one."""
        component, = ni.SIGNATURE_SCHEME_COMPONENTS
        parsed = ni.parse_component_formal(component.formal)

        self.assertEqual(parsed["curve"], "edwards25519")
        self.assertEqual(parsed["spec"], "RFC8032")


if __name__ == "__main__":
    unittest.main()
