"""`connect` must leave one client at a new peer, not two.

The first `GetPeerInfo` needs a client_id before the peer is registered, so one is
minted over the open channel. If it is not stored, `get_client_id_on_other_peer` finds
none for the peer and mints a second; the first stays orphaned there.
"""
import sqlite3
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.commands import connect as connect_module
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PEER = "peer-1"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConnectReusesMintedClientTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrate.create_tables(conn.cursor())
        conn.execute("INSERT INTO peer (id, advertisement, remote_client_id, balance_mu) "
                     "VALUES (?, ?, '', '0')", (PEER, b""))
        conn.commit()
        self._orig = SQLConnection._connection
        SQLConnection._connection = conn
        self.sc = SQLConnection()

    def tearDown(self):
        SQLConnection._connection = self._orig

    def test_the_minted_client_is_stored_for_the_peer(self):
        connect_module._keep_minted_client(PEER, "minted")
        self.assertEqual(self.sc.get_peer_client(peer_id=PEER), "minted")

    def test_a_client_already_stored_is_kept(self):
        self.sc.add_external_client(peer_id=PEER, client_id="bound")
        connect_module._keep_minted_client(PEER, "minted")
        self.assertEqual(self.sc.get_peer_client(peer_id=PEER), "bound")

    def test_no_minted_client_stores_nothing(self):
        connect_module._keep_minted_client(PEER, "")
        self.assertFalse(self.sc.get_peer_client(peer_id=PEER))

    def _connect(self, cert_id, info_answers, uri_known=False):
        """Run connect() against a fake peer; return (mint mock, get_peer_info mock, bee)."""
        channel = mock.MagicMock()
        peer_info = mock.MagicMock(public_key=cert_id, payment_contracts=[1])
        with mock.patch.object(connect_module, "SELF_ANNOUNCE_TO_CONNECTING_PEERS", True), \
             mock.patch.object(connect_module, "channel_and_peer_id", return_value=(channel, cert_id)), \
             mock.patch.object(connect_module, "node_channel", return_value=channel), \
             mock.patch.object(connect_module, "mint_client_id_on_channel", return_value="first") as mint, \
             mock.patch.object(connect_module, "BeeClient") as bee, \
             mock.patch.object(connect_module, "add_peer_instance", return_value=PEER), \
             mock.patch.object(connect_module, "associate_client_id_on_channel", return_value=True), \
             mock.patch("src.gateway.utils.generate_full_node_peer_info", return_value=mock.MagicMock()), \
             mock.patch.object(connect_module.sc, "uri_exists", return_value=uri_known), \
             mock.patch.object(connect_module.sc, "mark_peer_chosen"), \
             mock.patch.object(connect_module.sc, "claim_uri", return_value=[]), \
             mock.patch("src.manager.manager.peer_channel") as other_channel, \
             mock.patch("src.manager.manager._mint_client_over_channel") as second_mint:
            bee.get_peer_info.side_effect = [peer_info if a else None for a in info_answers]
            bee.introduce_peer.return_value = mock.MagicMock(token="OK")
            self.assertTrue(connect_module.connect("1.2.3.4:5"))
        second_mint.assert_not_called()
        other_channel.assert_not_called()
        return mint, bee

    def test_connect_announces_with_the_client_it_minted_first(self):
        mint, bee = self._connect(PEER, [True])
        mint.assert_called_once()
        self.assertEqual(bee.introduce_peer.call_args.kwargs["client_id"], "first")
        self.assertEqual(self.sc.get_peer_client(peer_id=PEER), "first")

    def test_reconnecting_a_known_peer_mints_nothing(self):
        self.sc.add_external_client(peer_id=PEER, client_id="bound")
        mint, bee = self._connect(PEER, [True], uri_known=True)
        mint.assert_not_called()
        self.assertEqual(bee.get_peer_info.call_args.kwargs["client_id"], "bound")
        self.assertEqual(bee.introduce_peer.call_args.kwargs["client_id"], "bound")
        self.assertEqual(self.sc.get_peer_client(peer_id=PEER), "bound")

    def test_a_stored_client_the_peer_ignores_falls_back_to_a_fresh_one(self):
        # Not replaced: no answer may be a timeout, and the stored one holds an account.
        self.sc.add_external_client(peer_id=PEER, client_id="bound")
        mint, bee = self._connect(PEER, [False, True], uri_known=True)
        mint.assert_called_once()
        self.assertEqual(bee.get_peer_info.call_count, 2)
        self.assertEqual(self.sc.get_peer_client(peer_id=PEER), "bound")


if __name__ == "__main__":
    unittest.main()
