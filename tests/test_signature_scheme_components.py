"""Unit tests for Peer.SignatureScheme as a stack of components (issue: signature
scheme changed from four fixed named tags to an open, unordered
``repeated Protocol components``, one Protocol per building block).

``src/reputation_system/node_identity.py`` only needs ``bip32`` (via
``bip_wallet_verification``) for wallet key derivation, which none of the functions
under test here touch -- so ``bip32`` is stubbed just enough to import the module for
real, mirroring tests/test_service_registry_load.py's approach, and un-stubbed again
once this file's tests are done.
"""
import sys
import types
import unittest

_STUBBED = {}


def _stub(name, **attrs):
    _STUBBED.setdefault(name, sys.modules.get(name))
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


try:
    import bip32  # noqa: F401
except ImportError:
    _stub("bip32", BIP32=type("BIP32", (), {}), HARDENED_INDEX=0x80000000)


def tearDownModule():
    for name, previous in _STUBBED.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


from protos import celaut_pb2 as celaut  # noqa: E402
from src.reputation_system import node_identity as ni  # noqa: E402


class NodeSignatureSchemeTests(unittest.TestCase):
    def test_builds_one_component_per_declared_building_block(self):
        scheme = ni.node_signature_scheme()
        self.assertEqual(len(scheme.components), len(ni.SIGNATURE_SCHEME_COMPONENTS))
        for component, (tags, prose) in zip(scheme.components, ni.SIGNATURE_SCHEME_COMPONENTS):
            self.assertEqual(tuple(component.tags), tags)
            self.assertEqual(component.prose, prose)
            self.assertEqual(component.formal, b"")

    def test_matches_itself(self):
        self.assertTrue(ni.same_signature_scheme(ni.node_signature_scheme(), ni.node_signature_scheme()))

    def test_declare_sets_the_field(self):
        peer = celaut.Peer()
        ni.declare_signature_scheme(peer)
        self.assertEqual(len(peer.signature_scheme.components), len(ni.SIGNATURE_SCHEME_COMPONENTS))

    def test_declare_without_prose_clears_it_on_every_component(self):
        peer = celaut.Peer()
        ni.declare_signature_scheme(peer, prose=False)
        for component in peer.signature_scheme.components:
            self.assertEqual(component.prose, "")
            self.assertTrue(component.tags, "tags must survive prose=False")


def _scheme(*components):
    scheme = celaut.Peer.SignatureScheme()
    for tags, formal in components:
        scheme.components.add(tags=list(tags), formal=formal)
    return scheme


class SameSignatureSchemeTests(unittest.TestCase):
    def test_identical_schemes_match(self):
        a = _scheme((["secp256k1"], b""), (["schnorr"], b""))
        b = _scheme((["secp256k1"], b""), (["schnorr"], b""))
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_component_order_does_not_matter(self):
        a = _scheme((["secp256k1"], b""), (["schnorr"], b""), (["blake2b256"], b""))
        b = _scheme((["blake2b256"], b""), (["secp256k1"], b""), (["schnorr"], b""))
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_a_shared_component_is_not_a_shared_scheme(self):
        # Same cardinality, one differing component (algorithm): must not match even
        # though curve and hash agree. This is the whole point of the refactor -- a
        # BIP-340 signer sharing the secp256k1 tag must not look compatible.
        a = _scheme((["secp256k1"], b""), (["schnorr"], b""), (["blake2b256"], b""))
        b = _scheme((["secp256k1"], b""), (["bip340"], b""), (["blake2b256"], b""))
        self.assertFalse(ni.same_signature_scheme(a, b))

    def test_different_cardinality_never_matches(self):
        a = _scheme((["secp256k1"], b""), (["schnorr"], b""))
        b = _scheme((["secp256k1"], b""), (["schnorr"], b""), (["blake2b256"], b""))
        self.assertFalse(ni.same_signature_scheme(a, b))

    def test_synonym_tags_within_one_component_match(self):
        a = _scheme((["secp256k1"], b""),)
        b = _scheme((["secp256k1", "K-256"], b""),)
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_formal_decides_over_tags_when_present(self):
        a = _scheme((["secp256k1"], b"spec-v1"),)
        b = _scheme((["secp256k1"], b"spec-v2"),)
        self.assertFalse(ni.same_signature_scheme(a, b))
        c = _scheme((["secp256k1"], b"spec-v1"),)
        self.assertTrue(ni.same_signature_scheme(a, c))

    def test_prose_is_never_compared(self):
        a = celaut.Peer.SignatureScheme()
        a.components.add(tags=["secp256k1"], prose="a description")
        b = celaut.Peer.SignatureScheme()
        b.components.add(tags=["secp256k1"], prose="a completely different wording")
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_empty_schemes_match(self):
        self.assertTrue(ni.same_signature_scheme(celaut.Peer.SignatureScheme(), celaut.Peer.SignatureScheme()))


class SpeaksOurSignatureSchemeTests(unittest.TestCase):
    def test_no_components_is_the_pre_field_default(self):
        peer = celaut.Peer()
        self.assertTrue(ni.speaks_our_signature_scheme(peer))

    def test_our_own_declared_scheme_speaks_our_scheme(self):
        peer = celaut.Peer()
        ni.declare_signature_scheme(peer)
        self.assertTrue(ni.speaks_our_signature_scheme(peer))

    def test_a_different_scheme_does_not(self):
        peer = celaut.Peer()
        peer.signature_scheme.components.add(tags=["secp256k1"])
        peer.signature_scheme.components.add(tags=["bip340"])
        self.assertFalse(ni.speaks_our_signature_scheme(peer))


if __name__ == "__main__":
    unittest.main()
