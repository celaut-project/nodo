"""``${VAR}`` placeholders in a ``Service.Network.formal`` (issue #385).

The grammar and the substitution, tested on their own before the three readers that
share them. What is pinned here:

* the grammar -- ``${NAME}`` with a C identifier, a value that is entirely a
  placeholder or none of one, and ``abc${X}`` reported as neither;
* **byte-identity when there is no template**, which is the compatibility promise the
  whole change rests on: a ``formal`` is hashed into a service id and compared byte
  for byte by ``match_networks``, so a body with no placeholder in it has to come out
  of ``substitute`` as the same object it went in as -- not re-sorted, not re-joined;
* every way of not completing a substitution collapsing to :class:`Missing` rather
  than an exception, because the launch path's whole contract is that a missing
  variable defers a network and never aborts a launch;
* the injection guard: a variable answered with a newline cannot add a key to
  somebody else's declaration.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from src.identity.node_identity import component_formal, parse_component_formal
    from src.manager.network_templates import (
        Missing,
        PLACEHOLDER,
        find_partial_placeholders,
        find_placeholders,
        has_placeholders,
        substitute,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR, f"network_templates unavailable: {IMPORT_ERROR}")
class PlaceholderGrammarTests(unittest.TestCase):
    def test_a_whole_value_placeholder_is_found_with_its_key(self):
        formal = component_formal(
            {"pow.chain": "ergo", "pow.block_id": "${ERGO_BLOCK_ID}"}
        )
        self.assertEqual(find_placeholders(formal), {"pow.block_id": "ERGO_BLOCK_ID"})

    def test_several_keys_may_be_templated_independently(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": "${B}",
                "pow.min_cumulative_difficulty": "${D}",
                "pow.max_tip_age_s": "3600",
            }
        )
        self.assertEqual(
            find_placeholders(formal),
            {"pow.block_id": "B", "pow.min_cumulative_difficulty": "D"},
        )

    def test_a_name_may_be_a_c_identifier_and_nothing_else(self):
        self.assertTrue(PLACEHOLDER.fullmatch("${_x}"))
        self.assertTrue(PLACEHOLDER.fullmatch("${A1_B2}"))
        # A leading digit, a dash and a dot are not identifiers, so they are not
        # placeholders and the value is a literal -- which is what an author who
        # wrote one would want, not a silent near-miss.
        self.assertIsNone(PLACEHOLDER.fullmatch("${1X}"))
        self.assertIsNone(PLACEHOLDER.fullmatch("${a-b}"))
        self.assertIsNone(PLACEHOLDER.fullmatch("${a.b}"))
        self.assertIsNone(PLACEHOLDER.fullmatch("$X"))
        self.assertIsNone(PLACEHOLDER.fullmatch("{X}"))

    def test_surrounding_whitespace_does_not_stop_a_value_being_a_placeholder(self):
        formal = b"pow.block_id= ${B} "
        self.assertEqual(find_placeholders(formal), {"pow.block_id": "B"})

    def test_a_partially_templated_value_is_neither_a_placeholder_nor_ignored(self):
        formal = component_formal({"pow.block_id": "abc${X}"})
        self.assertEqual(find_placeholders(formal), {})
        self.assertEqual(find_partial_placeholders(formal), ("pow.block_id",))
        self.assertTrue(has_placeholders(formal))

    def test_a_formal_with_no_template_has_none_of_either(self):
        formal = component_formal({"pow.chain": "ergo", "pow.block_id": "deadbeef"})
        self.assertEqual(find_placeholders(formal), {})
        self.assertEqual(find_partial_placeholders(formal), ())
        self.assertFalse(has_placeholders(formal))

    def test_an_unreadable_body_has_no_template_rather_than_raising(self):
        # Not this module's error to report: whoever actually consumes the body
        # reads it again and says so. What matters here is that it does not raise.
        self.assertEqual(find_placeholders(b"not a key=value body\nat all"), {})
        self.assertFalse(has_placeholders(b"\xff\xfe"))


@unittest.skipIf(IMPORT_ERROR, f"network_templates unavailable: {IMPORT_ERROR}")
class SubstituteTests(unittest.TestCase):
    def test_a_formal_with_no_template_comes_back_byte_identical(self):
        """The compatibility promise: an untemplated body is never rewritten.

        Deliberately built *unsorted and unpadded*, i.e. not the way
        ``component_formal`` would have written it, because that is exactly the case
        a round trip through parse/encode would silently "fix" -- and a fixed byte
        string is a different service id and a different ``match_networks`` verdict.
        """
        formal = b"pow.min_cumulative_difficulty=1\npow.chain=ergo\npow.block_id=ab"
        self.assertEqual(substitute(formal, {}), formal)
        self.assertEqual(substitute(formal, {"B": b"x"}), formal)

    def test_an_empty_formal_is_returned_untouched(self):
        self.assertEqual(substitute(b"", {}), b"")

    def test_every_placeholder_answered_gives_the_completed_body(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": "${B}",
                "pow.min_cumulative_difficulty": "${D}",
            }
        )
        result = substitute(formal, {"B": b"deadbeef", "D": b"1152921504606846976"})
        self.assertIsInstance(result, bytes)
        self.assertEqual(
            parse_component_formal(result),
            {
                "pow.chain": "ergo",
                "pow.block_id": "deadbeef",
                "pow.min_cumulative_difficulty": "1152921504606846976",
            },
        )

    def test_the_completed_body_is_canonical_whatever_order_it_was_written_in(self):
        a = b"pow.min_cumulative_difficulty=${D}\npow.chain=ergo"
        b = b"pow.chain=ergo\npow.min_cumulative_difficulty=${D}"
        self.assertEqual(substitute(a, {"D": b"7"}), substitute(b, {"D": b"7"}))

    def test_one_variable_may_answer_several_keys(self):
        formal = component_formal({"a": "${V}", "b": "${V}"})
        self.assertEqual(
            parse_component_formal(substitute(formal, {"V": b"same"})),
            {"a": "same", "b": "same"},
        )

    def test_a_missing_variable_is_reported_and_not_raised(self):
        formal = component_formal({"pow.block_id": "${B}", "pow.chain": "ergo"})
        result = substitute(formal, {})
        self.assertIsInstance(result, Missing)
        self.assertEqual(result.variables, ("B",))
        self.assertIn("B", str(result))

    def test_every_missing_variable_is_named_not_just_the_first(self):
        formal = component_formal({"a": "${X}", "b": "${Y}", "c": "${Z}"})
        result = substitute(formal, {"Y": b"1"})
        self.assertEqual(result.variables, ("X", "Z"))

    def test_a_partially_templated_value_is_missing_as_malformed(self):
        formal = component_formal({"pow.block_id": "abc${X}"})
        result = substitute(formal, {"X": b"deadbeef"})
        self.assertIsInstance(result, Missing)
        self.assertTrue(result.malformed)
        self.assertIn("pow.block_id", result.malformed[0])

    def test_a_value_that_is_not_utf8_cannot_answer_a_placeholder(self):
        formal = component_formal({"k": "${V}"})
        result = substitute(formal, {"V": b"\xff\xfe"})
        self.assertIsInstance(result, Missing)
        self.assertTrue(result.malformed)

    def test_a_value_carrying_a_newline_cannot_add_a_key_to_the_declaration(self):
        """The injection guard.

        ``formal`` separates pairs with newlines. Were a newline allowed through,
        an instantiator answering one variable would be *appending a key* to
        somebody else's declaration -- and the firewall would open whatever that
        added key resolved to.
        """
        formal = component_formal(
            {"pow.chain": "ergo", "pow.min_cumulative_difficulty": "${D}"}
        )
        result = substitute(formal, {"D": b"0\npow.block_id=deadbeef"})
        self.assertIsInstance(result, Missing)
        self.assertTrue(result.malformed)

    def test_a_value_may_contain_an_equals_sign(self):
        # `parse_component_formal` splits on the first `=`, so a value may hold one.
        formal = component_formal({"k": "${V}"})
        self.assertEqual(
            parse_component_formal(substitute(formal, {"V": b"a=b"})), {"k": "a=b"}
        )


if __name__ == "__main__":
    unittest.main()
