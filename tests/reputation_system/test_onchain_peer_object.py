"""What a node publishes about itself on-chain (R9), for issue #236.

It publishes a signed ``Peer``, not a bare ``Instance``: the envelope carries the
node's identity, the signature over its addresses and the address-expiry estimate.
The point is that it is verifiable *against the same box* -- R7 holds the owner
propositionBytes, which are ``0008cd`` + the same public key -- so a reader can trust
the published address and its expiry without ever contacting the node.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from google.protobuf.json_format import Parse

    from protos import celaut_pb2
    from src.utils import node_identity as ni
    import src.reputation_system.contracts.ergo.transaction as tx
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PUBLIC_IP = "93.184.216.34"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnChainPeerObjectTests(unittest.TestCase):
    def _overrides(self, validity):
        """Override only the keys under test; everything else (notably the identity
        mnemonic) must still come from the real config, or nothing gets signed."""
        from src.utils.config import ConfigManager

        real_get = ConfigManager.get
        manager = ConfigManager()
        overrides = {
            "network.PUBLIC_IP": PUBLIC_IP,
            "network.ADDRESS_VALIDITY_SECONDS": validity,
            "GATEWAY_PORT": 8080,
        }

        def fake_get(key, default=None):
            if key in overrides:
                return overrides[key]
            return real_get(manager, key, default)

        return fake_get

    def _publish(self, validity=86400):
        fake_get = self._overrides(validity)
        with mock.patch.object(tx, "SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", lambda: True), \
             mock.patch.object(tx.env_manager, "get", side_effect=fake_get), \
             mock.patch("src.utils.config.ConfigManager.get", autospec=True,
                        side_effect=lambda _self, key, default=None: fake_get(key, default)), \
             mock.patch("src.utils.network.get_local_ip", return_value=PUBLIC_IP):
            return Parse(tx._self_network_data(), celaut_pb2.Peer())

    def _r7_owner_key(self):
        """The public key a reader would recover from the box's R7 owner."""
        return ni.node_proposition_hex(ni.get_node_public_key_hex())[len("0008cd"):]

    def _payload_for(self, peer):
        return ni.canonical_peer_payload(
            peer.public_key,
            peer.ts,
            ni.canonical_peer_content_digest(peer),
        )

    def test_publishes_the_address(self):
        peer = self._publish()
        self.assertEqual(peer.uri[0].ip, PUBLIC_IP)

    def test_publishes_the_transport_of_the_address(self):
        # A reader has to know which kind of socket the advertised address takes.
        self.assertEqual(list(self._publish().uri[0].transport.tags), ["tcp"])

    def test_a_downgraded_transport_breaks_the_signature(self):
        peer = self._publish()
        del peer.uri[0].transport.tags[:]
        peer.uri[0].transport.tags.append("udp")
        self.assertFalse(
            ni.verify_peer_payload(self._r7_owner_key(), self._payload_for(peer), peer.signature)
        )

    def test_publishes_a_peer_envelope_not_a_bare_instance(self):
        peer = self._publish()
        self.assertTrue(peer.public_key)
        self.assertTrue(peer.signature)
        self.assertTrue(peer.ts)

    def test_publishes_the_expiry_estimate(self):
        peer = self._publish(validity=86400)
        self.assertEqual(peer.uri[0].expiry_unix_timestamp - peer.ts, 86400)

    def test_no_expiry_is_published_when_none_is_configured(self):
        self.assertEqual(self._publish(validity=0).uri[0].expiry_unix_timestamp, 0)

    def test_the_published_key_is_the_r7_owner(self):
        # One mnemonic per node, so the identity signing R9 is the wallet owning R7.
        self.assertEqual(self._publish().public_key, self._r7_owner_key())

    def test_signature_verifies_against_r7_without_contacting_the_node(self):
        peer = self._publish()
        self.assertTrue(
            ni.verify_peer_payload(self._r7_owner_key(), self._payload_for(peer), peer.signature)
        )

    def test_a_stretched_expiry_breaks_the_signature(self):
        # Whoever relays the on-chain data must not be able to make a soon-to-expire
        # address look durable, nor strip the estimate.
        peer = self._publish()
        peer.uri[0].expiry_unix_timestamp += 999_999
        self.assertFalse(
            ni.verify_peer_payload(self._r7_owner_key(), self._payload_for(peer), peer.signature)
        )

    def test_a_swapped_address_breaks_the_signature(self):
        peer = self._publish()
        peer.uri[0].ip = "6.6.6.6"
        self.assertFalse(
            ni.verify_peer_payload(self._r7_owner_key(), self._payload_for(peer), peer.signature)
        )

    def test_stays_small_enough_for_a_register(self):
        # An Ergo box is a few KB in total; R9 must not grow unbounded, which is why
        # only the gateway URI is published and not the whole GetPeerInfo instance.
        fake_get = self._overrides(86400)
        with mock.patch.object(tx, "SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", lambda: True), \
             mock.patch.object(tx.env_manager, "get", side_effect=fake_get), \
             mock.patch("src.utils.network.get_local_ip", return_value=PUBLIC_IP):
            self.assertLess(len(tx._self_network_data()), 1024)

    def test_opt_out_publishes_nothing(self):
        with mock.patch.object(tx, "SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF", lambda: False):
            self.assertEqual(tx._self_network_data(), tx.NO_NETWORK_ADDRESS)


if __name__ == "__main__":
    unittest.main()
