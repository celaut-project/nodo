"""Peer registration under node identity (#236): what a signed announcement may and
may not do to our stored view of a peer.

These pin the defects found reviewing the first cut of the implementation: a relayed
announcement with a swapped payment contract, and a superseded address never being
dropped. They also pin that an identity is *mandatory* -- an announcement the node
cannot attribute to a key is refused outright rather than registered under a random id.
"""
import os
import sqlite3
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from mnemonic import Mnemonic

    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.identity import node_identity as ni
    from src.reputation_system.bip_wallet_verification import (
        bip_schnorr_sign,
        derive_compressed_pubkey,
    )
    import src.manager.manager as manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerIdentityRegistrationTests(unittest.TestCase):
    def setUp(self):
        # A file-backed DB: parts of the registration path go through
        # query_interface.fetch_query, which opens its own connection to DATABASE_FILE
        # and cannot see an in-memory one.
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self._orig_db = manager.sc  # keep the module-level singleton reference
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        migrate.create_tables(self.conn.cursor())
        self.conn.commit()
        self._orig_conn = SQLConnection._connection
        SQLConnection._connection = self.conn

        import src.database.query_interface as qi
        self._orig_qi_db = qi.DATABASE_FILE
        qi.DATABASE_FILE = self.db_path

        self.mnemonic = Mnemonic("english").generate(strength=128)
        self.pubkey, self._private_key = ni._cached_keypair(self.mnemonic)

    def tearDown(self):
        import src.database.query_interface as qi
        qi.DATABASE_FILE = self._orig_qi_db
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def _sign(self, peer, ts=100):
        """Sign ``peer`` as it stands, the way a real node does.

        Everything the announcement declares -- the scheme included -- is inside the
        digest, so a peer signs what it has actually written down. A test that wants a
        different declaration changes it *before* this, not after: changing it after is
        precisely the tampering the signature exists to catch.
        """
        peer.public_key, peer.ts = self.pubkey, ts
        peer.signature = self._private_key.sign(
            ni.canonical_peer_payload(
                self.pubkey, ts, ni.canonical_peer_content_digest(peer)
            ).encode("utf-8")
        ).hex()
        return peer

    def _peer(
        self, uris, *, signed=True, ts=100, contract=b"HONEST", transport="tcp",
        prepare=None,
    ):
        peer = celaut_pb2.Peer()
        for ip, port in uris:
            uri = peer.uri.add(ip=ip, port=port)
            uri.transport.tags.append(transport)
        mu_per_unit = peer.payment_contracts.add()
        mu_per_unit.contract.ledger.formal = contract
        mu_per_unit.mu_per_unit.n = "1"
        if prepare:
            prepare(peer)
        if signed:
            self._sign(peer, ts)
        return peer

    def _uris(self, peer_id):
        return sorted(
            row[0] for row in self.conn.execute(
                "SELECT ip FROM uri WHERE peer_id = ?", (peer_id,)
            ).fetchall()
        )

    def test_signed_peer_is_identified_by_its_public_key(self):
        self.assertEqual(manager.add_peer_instance(self._peer([("10.0.0.1", 9999)])), self.pubkey)

    def test_swapped_payment_contract_is_rejected(self):
        peer = self._peer([("10.0.0.1", 9999)])
        peer.payment_contracts[0].contract.ledger.formal = b"ATTACKER"
        self.assertIsNone(manager.verified_peer_public_key(peer))

    def test_injected_address_is_rejected(self):
        peer = self._peer([("10.0.0.1", 9999)])
        peer.uri.add(ip="6.6.6.6", port=9999)
        self.assertIsNone(manager.verified_peer_public_key(peer))

    def test_downgraded_transport_is_rejected(self):
        # Flipping an address's transport would send a reader at it with the wrong
        # kind of socket, so it must not survive the signature check.
        peer = self._peer([("10.0.0.1", 9999)])
        del peer.uri[0].transport.tags[:]
        peer.uri[0].transport.tags.append("udp")
        self.assertIsNone(manager.verified_peer_public_key(peer))

    def test_a_forged_message_cannot_hijack_an_identified_peer(self):
        # A peer's addresses are public (GetPeerInfo serves them, and they go
        # on-chain). Naming one must not let a stranger rewrite that peer's
        # advertisement or payment contracts without signing for its identity.
        self.assertEqual(
            manager.add_peer_instance(self._peer([("10.0.0.1", 9999)])), self.pubkey
        )
        honest = self.conn.execute(
            "SELECT advertisement FROM peer WHERE id=?", (self.pubkey,)
        ).fetchone()[0]

        forged = self._peer([("10.0.0.1", 9999)], signed=False, contract=b"ATTACKER")
        forged.public_key, forged.signature = self.pubkey, "not-a-signature"
        manager.add_peer_instance(forged)

        self.assertEqual(
            self.conn.execute(
                "SELECT advertisement FROM peer WHERE id=?", (self.pubkey,)
            ).fetchone()[0],
            honest,
            "an unsigned announcement overwrote a verified peer's advertisement",
        )

    def test_an_address_whose_transport_is_unusable_is_not_kept(self):
        # Storing it would leave a row the node can never speak to, and pruning must
        # not resurrect it just because the announcement listed it.
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=100))
        manager.add_peer_instance(
            self._peer([("5.6.7.8", 9999)], ts=200, transport="carrier-pigeon")
        )
        self.assertEqual(self._uris(self.pubkey), [])

    def test_the_declared_transport_is_stored(self):
        peer_id = manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], transport="udp"))
        self.assertEqual(
            self.conn.execute(
                "SELECT transport FROM uri WHERE peer_id = ?", (peer_id,)
            ).fetchone()[0],
            "udp",
        )

    def test_an_unsigned_peer_is_not_registered_at_all(self):
        # An identity is mandatory: there is no id to give a peer that does not sign,
        # and the uuid4 fallback that used to name one accepted peers nobody could
        # authenticate, at an address anyone can claim.
        self.assertIsNone(manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], signed=False)))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM peer").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM uri").fetchone()[0], 0)

    def test_a_badly_signed_peer_is_not_registered_at_all(self):
        peer = self._peer([("10.0.0.1", 9999)], signed=False)
        peer.public_key, peer.ts, peer.signature = self.pubkey, 100, "not-a-signature"
        self.assertIsNone(manager.add_peer_instance(peer))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM peer").fetchone()[0], 0)

    def test_a_peer_declaring_our_signature_scheme_is_registered(self):
        # The cryptography a node speaks, spelled out rather than assumed. Declaring
        # it must be the same announcement as declaring nothing, or upgrading would
        # split the network in two.
        peer = self._peer([("10.0.0.1", 9999)], prepare=ni.declare_signature_scheme)
        self.assertEqual(manager.add_peer_instance(peer), self.pubkey)

    def test_a_peer_signing_with_another_scheme_is_refused(self):
        # A valid signature over the same payload, by the same key -- and still
        # refused, because the peer says those bytes are something this node cannot
        # verify. Accepting it would mean trusting a signature nobody checked.
        def _other_scheme(peer):
            peer.signature_scheme.components.add(tags=["ed25519"])
            peer.signature_scheme.components.add(tags=["ed25519ph"])

        peer = self._peer([("10.0.0.1", 9999)], prepare=_other_scheme)
        self.assertIsNone(manager.verified_peer_public_key(peer))
        self.assertIsNone(manager.add_peer_instance(peer))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM peer").fetchone()[0], 0)

    def test_a_shared_component_is_not_a_shared_scheme(self):
        # Same cardinality, a shared tag, and still refused: a peer naming the pre-hashed
        # variant of RFC 8032 alongside the pure one produces signatures this node cannot
        # read. The pairing must be total, not "most components matched" -- the laxer
        # match the rest of the codebase uses for ledgers would accept it.
        def _extra_tag(peer):
            ni.declare_signature_scheme(peer)
            peer.signature_scheme.components[0].tags.append("ed25519ph")

        self.assertIsNone(
            manager.verified_peer_public_key(
                self._peer([("10.0.0.1", 9999)], prepare=_extra_tag)
            )
        )

        # Rebuilding the same components in reverse order still matches: order is not
        # part of what a descriptor says (Peer.SignatureScheme.components is explicitly
        # unordered).
        def _reversed(peer):
            for declared in reversed(ni.SIGNATURE_SCHEME_COMPONENTS):
                peer.signature_scheme.components.add(
                    tags=list(declared.tags), prose=declared.prose, formal=declared.formal
                )

        self.assertEqual(
            manager.verified_peer_public_key(
                self._peer([("10.0.0.1", 9999)], prepare=_reversed)
            ),
            self.pubkey,
            "component order is not part of what a descriptor says",
        )

    def test_rewording_the_description_is_not_a_different_scheme(self):
        # prose is human text with no agreed wording; refusing a peer over it would
        # make every edit to that sentence a network split. The peer signs its own
        # wording, so this stays a question about the comparison and not about the
        # signature.
        def _reworded(peer):
            ni.declare_signature_scheme(peer)
            peer.signature_scheme.components[0].prose = "however this peer words it"

        peer = self._peer([("10.0.0.1", 9999)], prepare=_reworded)
        self.assertEqual(manager.verified_peer_public_key(peer), self.pubkey)

    def test_rewording_the_description_of_a_signed_scheme_is_tampering(self):
        # The other half of the same rule: prose is not part of the *comparison*, but
        # it is part of what the peer signed, so a relay cannot rewrite the sentence a
        # reader is shown.
        peer = self._peer([("10.0.0.1", 9999)], prepare=ni.declare_signature_scheme)
        peer.signature_scheme.components[0].prose = "reworded in transit"
        self.assertIsNone(manager.verified_peer_public_key(peer))

    def test_a_formal_specification_decides_over_the_tags(self):
        # Nothing publishes one yet (ours is empty, like the Ergo ledger's), but when
        # one side names an artifact for a component the tags stop being what the
        # answer for that component rests on.
        def _with_formal(peer):
            ni.declare_signature_scheme(peer)
            peer.signature_scheme.components[0].formal = b"some formal specification"

        self.assertIsNone(
            manager.verified_peer_public_key(
                self._peer([("10.0.0.1", 9999)], prepare=_with_formal)
            )
        )

    def test_the_scheme_is_an_unordered_stack_of_descriptors(self):
        # Pins the shape: an unordered stack of tags/prose/formal components, the way
        # every other replaceable component in celaut is declared -- not a hash of any
        # of it, and not four fixed named fields either.
        scheme = ni.node_signature_scheme()
        self.assertEqual(len(scheme.components), len(ni.SIGNATURE_SCHEME_COMPONENTS))
        for component, declared in zip(scheme.components, ni.SIGNATURE_SCHEME_COMPONENTS):
            self.assertEqual(tuple(component.tags), declared.tags)
            self.assertEqual(component.prose, declared.prose)
            self.assertEqual(component.formal, declared.formal)
        self.assertTrue(ni.same_signature_scheme(scheme, ni.node_signature_scheme()))

    def test_an_announcement_without_a_scheme_still_verifies(self):
        # What every node sent before the field existed. It has exactly one meaning,
        # so it keeps it -- an empty descriptor is the default, never a wildcard.
        peer = self._peer([("10.0.0.1", 9999)])
        self.assertFalse(peer.HasField("signature_scheme"))
        self.assertEqual(manager.verified_peer_public_key(peer), self.pubkey)

    def test_every_stored_peer_id_is_a_public_key(self):
        for signed in (False, True):
            manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], signed=signed))
        ids = [row[0] for row in self.conn.execute("SELECT id FROM peer").fetchall()]
        self.assertEqual(ids, [self.pubkey])
        for peer_id in ids:
            self.assertIsNotNone(ni.normalize_public_key_hex(peer_id))

    def test_accept_peer_refresh_requires_a_signature_from_that_identity(self):
        # This runs immediately before `pay` sends money and feeds payment_contracts
        # straight into the DB, so whoever answers at the address must prove they are
        # the peer we meant to reach -- the channel has no TLS.
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100))

        self.assertTrue(
            manager.accept_peer_refresh(self._peer([("1.2.3.4", 9999)], ts=300), self.pubkey)
        )
        self.assertFalse(
            manager.accept_peer_refresh(
                self._peer([("9.9.9.9", 9999)], signed=False), self.pubkey
            ),
            "an unsigned GetPeerInfo response was accepted as a peer's own",
        )
        forged = self._peer([("9.9.9.9", 9999)], signed=False)
        forged.public_key, forged.ts, forged.signature = self.pubkey, 400, "not-a-signature"
        self.assertFalse(manager.accept_peer_refresh(forged, self.pubkey))
        self.assertFalse(
            manager.accept_peer_refresh(self._peer([("1.2.3.4", 9999)], ts=200), self.pubkey),
            "a stale ts was accepted",
        )
        self.assertEqual(self._uris(self.pubkey), ["1.2.3.4"])

    def test_a_newer_signed_announcement_drops_superseded_addresses(self):
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100))
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])

    def test_several_addresses_in_one_announcement_are_all_kept(self):
        manager.add_peer_instance(
            self._peer([("5.6.7.8", 9999), ("9.9.9.9", 9999)], ts=100)
        )
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8", "9.9.9.9"])

    def test_a_replayed_announcement_cannot_prune_addresses(self):
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200))
        # An attacker replays an older, genuinely signed message to push the peer
        # back to a stale address.
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])

    def test_registration_is_idempotent(self):
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=100))
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM uri WHERE peer_id=?", (self.pubkey,)
            ).fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
