"""Naming and resolution invariants for the algorithm registry in hashing.py.

Canonical names are family_digestbits (sha2_256, sha3_256, shake_256,
blake2b_256), matching the shape sha3_256 and shake_256 already had. "sha256"
alone and "blake2b" alone were ambiguous or incomplete on that axis: nothing
in the name says which SHA family, or that this node only ever asks BLAKE2b
for a 256-bit digest (it supports others). Older/shorter aliases keep
resolving so an existing config.yaml is never invalidated by this.
"""
import unittest

from src.utils.hashing import (
    BLAKE2B_ID,
    HASH_NAME_TO_ID,
    HASH_SPECS,
    SHA256_ID,
    SHA3_256_ID,
    SHAKE_256_ID,
    resolve_hash_config,
)


class CanonicalNamesTests(unittest.TestCase):
    def test_names_are_family_digestbits(self):
        self.assertEqual(HASH_SPECS[SHA256_ID].name, "sha2_256")
        self.assertEqual(HASH_SPECS[SHA3_256_ID].name, "sha3_256")
        self.assertEqual(HASH_SPECS[SHAKE_256_ID].name, "shake_256")
        self.assertEqual(HASH_SPECS[BLAKE2B_ID].name, "blake2b_256")

    def test_every_canonical_name_resolves_back_to_its_own_spec(self):
        # What a report, an error message or the TUI shows for a configured
        # algorithm must be exactly what a user can type back in -- otherwise
        # displaying the name and re-entering it unchanged would fail to parse.
        for hash_id, spec in HASH_SPECS.items():
            with self.subTest(name=spec.name):
                self.assertIn(spec.name, HASH_NAME_TO_ID)
                self.assertEqual(resolve_hash_config(spec.name).id_bytes, hash_id)

    def test_every_name_is_its_own_spec_digest_size_and_hasher(self):
        # A resolved name must be usable, not just present in the map.
        for hash_id, spec in HASH_SPECS.items():
            with self.subTest(name=spec.name):
                resolved = resolve_hash_config(spec.name)
                self.assertEqual(resolved.digest_size, spec.digest_size)
                self.assertIs(resolved.hasher_factory, spec.hasher_factory)


class OlderAliasesStillResolveTests(unittest.TestCase):
    """A config.yaml written before the rename must keep working, unedited."""

    CASES = (
        ("sha256", SHA256_ID),
        ("sha2_256", SHA256_ID),
        ("sha3", SHA3_256_ID),
        ("sha3_256", SHA3_256_ID),
        ("shake", SHAKE_256_ID),
        ("shake_256", SHAKE_256_ID),
        ("blake2", BLAKE2B_ID),
        ("blake2b", BLAKE2B_ID),
        ("blake2b_256", BLAKE2B_ID),
    )

    def test_every_alias_resolves_to_the_expected_algorithm(self):
        for alias, expected_id in self.CASES:
            with self.subTest(alias=alias):
                self.assertEqual(resolve_hash_config(alias).id_bytes, expected_id)

    def test_resolution_is_case_insensitive(self):
        for alias, expected_id in self.CASES:
            with self.subTest(alias=alias):
                self.assertEqual(
                    resolve_hash_config(alias.upper()).id_bytes, expected_id)

    def test_a_hex_hash_id_still_resolves_directly(self):
        # The one accepted form that is not, and cannot be, an enumerable name.
        self.assertEqual(
            resolve_hash_config(SHA3_256_ID.hex()).id_bytes, SHA3_256_ID)

    def test_an_unrecognised_name_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_hash_config("md5")

    def test_a_well_formed_but_unknown_hash_id_lists_the_canonical_names(self):
        # Only a value that parses as hex but names no known algorithm reaches
        # this branch of resolve_hash_config; an unrecognised bare name (the
        # case above) is rejected before it gets this far.
        with self.assertRaises(ValueError) as caught:
            resolve_hash_config("00" * 32)
        message = str(caught.exception)
        for spec in HASH_SPECS.values():
            self.assertIn(spec.name, message)


if __name__ == "__main__":
    unittest.main()
