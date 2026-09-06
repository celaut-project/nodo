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
from unittest.mock import patch

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
from src.identity import node_identity as ni  # noqa: E402


class NodeSignatureSchemeTests(unittest.TestCase):
    def test_builds_one_component_per_declared_building_block(self):
        scheme = ni.node_signature_scheme()
        self.assertEqual(len(scheme.components), len(ni.SIGNATURE_SCHEME_COMPONENTS))
        for component, declared in zip(scheme.components, ni.SIGNATURE_SCHEME_COMPONENTS):
            self.assertEqual(tuple(component.tags), declared.tags)
            self.assertEqual(component.prose, declared.prose)
            self.assertEqual(component.formal, declared.formal)

    def test_no_formal_is_shared_between_two_components(self):
        # `formal` decides on its own wherever both sides carry one, so two components
        # holding the same value are interchangeable -- and a peer repeating that value
        # on as many components would then match whatever its tags said. Empty ones are
        # the "nothing determinate to point at" default and are compared by tags.
        formals = [c.formal for c in ni.SIGNATURE_SCHEME_COMPONENTS if c.formal]
        self.assertEqual(len(formals), len(set(formals)))

    def test_the_formal_states_the_parameters_the_prose_describes(self):
        # The two are one declaration at two levels of detail: a reader implements from
        # the prose, a comparison reads the formal, and they must not drift apart.
        component, = ni.SIGNATURE_SCHEME_COMPONENTS
        declared = dict(
            line.split("=", 1) for line in component.formal.decode().splitlines()
        )
        self.assertEqual(declared["curve"], "edwards25519")
        self.assertEqual(declared["variant"], "pure")
        self.assertEqual(declared["prehash"], "none")
        self.assertEqual(declared["spec"], "RFC8032")
        self.assertEqual(declared["public_key_bytes"], "32")
        self.assertEqual(declared["signature_bytes"], "64")

    def test_the_formal_is_canonical(self):
        # Byte-compared and covered by the announcement's signature, so it cannot
        # depend on the order the parameters were written in.
        lines = ni.SIGNATURE_SCHEME_COMPONENTS[0].formal.decode().splitlines()
        self.assertEqual(lines, sorted(lines))

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

    def test_one_shared_tag_is_enough(self):
        # Tags are alternative names for the single thing a component names, so two
        # components agreeing on any one of them have named the same thing twice.
        # Spelling it with one synonym more is not naming something else.
        a = _scheme((["secp256k1"], b""),)
        b = _scheme((["secp256k1", "K-256"], b""),)
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_no_shared_tag_is_a_different_component(self):
        a = _scheme((["secp256k1"], b""),)
        b = _scheme((["ed25519"], b""),)
        self.assertFalse(ni.same_signature_scheme(a, b))

    def test_a_tag_beside_ours_does_not_unsettle_what_our_formal_pins(self):
        # A peer writing the pre-hashed RFC 8032 tag beside the pure one, and pointing
        # at the same parameters this node declares. What it announces IS this node's
        # scheme; the extra word is vocabulary.
        ours = ni.node_signature_scheme()
        theirs = ni.node_signature_scheme()
        theirs.components[0].tags.append("ed25519ph")
        self.assertTrue(ni.same_signature_scheme(ours, theirs))

    def test_a_conflicting_tag_is_told_apart_by_the_formal_and_not_by_the_tags(self):
        # The price of matching on one shared tag, and the way out of it. Bare tags
        # cannot separate the pre-hashed variant from the pure one when a peer names
        # both -- same curve, different message convention -- so a component that needs
        # them told apart states its parameters, and then they are.
        pure, prehashed = _scheme((["ed25519"], b"")), _scheme((["ed25519", "ed25519ph"], b""))
        self.assertTrue(ni.same_signature_scheme(pure, prehashed))

        pinned = ni.node_signature_scheme()
        theirs = ni.node_signature_scheme()
        theirs.components[0].formal = b"prehash=sha512\n"
        self.assertFalse(ni.same_signature_scheme(pinned, theirs))

    def test_tags_carry_no_order(self):
        a = _scheme((["secp256k1", "K-256"], b""),)
        b = _scheme((["K-256", "secp256k1"], b""),)
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_formal_decides_when_both_sides_declare_one(self):
        a = _scheme((["secp256k1"], b"spec-v1"),)
        b = _scheme((["secp256k1"], b"spec-v2"),)
        self.assertFalse(ni.same_signature_scheme(a, b))
        c = _scheme((["secp256k1"], b"spec-v1"),)
        self.assertTrue(ni.same_signature_scheme(a, c))

    def test_a_formal_on_one_side_alone_leaves_the_tags_deciding(self):
        # Declaring the parameters says more than staying quiet about them; it does not
        # contradict a peer that stayed quiet. Otherwise pinning a component down would
        # cut this node off from everyone naming the same thing without pinning it.
        a = _scheme((["secp256k1"], b"spec-v1"),)
        b = _scheme((["secp256k1"], b""),)
        self.assertTrue(ni.same_signature_scheme(a, b))
        self.assertFalse(ni.same_signature_scheme(a, _scheme((["ed25519"], b""),)))

    def test_a_formal_alone_shares_nothing_with_tags_alone(self):
        # One states parameters and no name, the other a name and no parameters:
        # nothing the two said can be compared.
        a = _scheme(([], b"spec-v1"),)
        b = _scheme((["secp256k1"], b""),)
        self.assertFalse(ni.same_signature_scheme(a, b))

    def test_prose_is_never_compared(self):
        a = celaut.Peer.SignatureScheme()
        a.components.add(tags=["secp256k1"], prose="a description")
        b = celaut.Peer.SignatureScheme()
        b.components.add(tags=["secp256k1"], prose="a completely different wording")
        self.assertTrue(ni.same_signature_scheme(a, b))

    def test_empty_schemes_match(self):
        self.assertTrue(ni.same_signature_scheme(celaut.Peer.SignatureScheme(), celaut.Peer.SignatureScheme()))


class UndeclaredComponentTests(unittest.TestCase):
    """A component must say what it is: tags, formal, or both -- never neither."""

    def test_tags_alone_are_a_declaration(self):
        a = _scheme((["secp256k1"], b""),)
        self.assertTrue(ni.same_signature_scheme(a, _scheme((["secp256k1"], b""),)))

    def test_formal_alone_is_a_declaration(self):
        a = _scheme(([], b"spec-v1"),)
        self.assertTrue(ni.same_signature_scheme(a, _scheme(([], b"spec-v1"),)))

    def test_formal_alone_does_not_match_a_different_formal(self):
        a = _scheme(([], b"spec-v1"),)
        self.assertFalse(ni.same_signature_scheme(a, _scheme(([], b"spec-v2"),)))

    def test_neither_tags_nor_formal_never_matches(self):
        undeclared = celaut.Peer.SignatureScheme()
        undeclared.components.add(prose="a description and nothing else")
        # Not even against a byte-identical copy of itself: something is missing from
        # that component, and "is this the cryptography I speak?" has one safe answer.
        other = celaut.Peer.SignatureScheme()
        other.components.add(prose="a description and nothing else")
        self.assertFalse(ni.same_signature_scheme(undeclared, other))

    def test_one_undeclared_component_sinks_the_whole_scheme(self):
        a = _scheme((["secp256k1"], b""), (["schnorr"], b""))
        b = celaut.Peer.SignatureScheme()
        b.components.add(tags=["secp256k1"])
        b.components.add(prose="the algorithm, described but not named")
        self.assertFalse(ni.same_signature_scheme(a, b))

    def test_a_peer_with_an_undeclared_component_does_not_speak_our_scheme(self):
        peer = celaut.Peer()
        ni.declare_signature_scheme(peer)
        peer.signature_scheme.components[0].ClearField("tags")
        peer.signature_scheme.components[0].ClearField("formal")
        self.assertFalse(ni.speaks_our_signature_scheme(peer))

    def test_a_peer_keeping_only_the_formal_still_speaks_our_scheme(self):
        # Half of that declaration is a declaration: the parameters name the component
        # on their own, which is what naming it with parameters is for.
        peer = celaut.Peer()
        ni.declare_signature_scheme(peer)
        peer.signature_scheme.components[0].ClearField("tags")
        self.assertTrue(ni.speaks_our_signature_scheme(peer))


class ComponentCapTests(unittest.TestCase):
    """The pairing search is factorial in a length the peer picks; the cap bounds it."""

    @staticmethod
    def _config(value):
        # Patches the name node_identity resolves, so the real read path
        # (ConfigManager().get(KEY, default)) is the one under test.
        return patch.object(
            ni, "ConfigManager",
            lambda: types.SimpleNamespace(get=lambda key, default=None: value)
        )

    def _n_components(self, n):
        return _scheme(*(([f"tag{i}"], b"") for i in range(n)))

    def test_a_scheme_at_the_cap_is_still_compared(self):
        with self._config(5):
            self.assertTrue(ni.same_signature_scheme(self._n_components(5), self._n_components(5)))

    def test_a_scheme_over_the_cap_is_refused_rather_than_computed(self):
        with self._config(5):
            self.assertFalse(ni.same_signature_scheme(self._n_components(6), self._n_components(6)))

    def test_the_cap_is_configurable(self):
        with self._config(6):
            self.assertTrue(ni.same_signature_scheme(self._n_components(6), self._n_components(6)))

    def test_an_unusable_cap_falls_back_to_the_default(self):
        # Zero would refuse every scheme, including this node's own.
        for unusable in (0, -1, "", None, "many"):
            with self.subTest(configured=unusable), self._config(unusable):
                self.assertEqual(
                    ni._max_signature_scheme_components(),
                    ni.DEFAULT_MAX_SIGNATURE_SCHEME_COMPONENTS,
                )

    def test_our_own_scheme_fits_under_the_default_cap(self):
        self.assertLessEqual(
            len(ni.SIGNATURE_SCHEME_COMPONENTS), ni.DEFAULT_MAX_SIGNATURE_SCHEME_COMPONENTS
        )


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
