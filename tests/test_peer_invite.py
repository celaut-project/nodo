"""A peer invitation is small enough for a QR code and pins the identity it is for (#572)."""
import os
import tempfile
import unittest
from unittest import mock

from tests.config_bootstrap import load_example_config

load_example_config()

IMPORT_ERROR = None
try:
    from protos import celaut_pb2, invite_pb2
    from src.utils import peer_invite as pi
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

KEY = bytes(range(32))
ONION_KEY = bytes(range(100, 132))


def _peer(*addresses):
    peer = celaut_pb2.Peer(public_key=KEY.hex())
    for ip, port in addresses:
        peer.uri.add(ip=ip, port=port)
    return peer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerInviteTests(unittest.TestCase):
    def test_round_trip_for_each_tag(self):
        onion = pi._onion_text(ONION_KEY)
        peer = _peer(("203.0.113.7", 8090), ("2001:db8::1", 8091), (onion, 8092))
        invite = pi.parse_invite(pi.build_invite(peer))
        self.assertEqual(invite.public_key, KEY)
        self.assertEqual(
            [list(a.tags) for a in invite.addr], [["ip4"], ["ip6"], ["tor3"]]
        )
        self.assertEqual(
            pi.invite_targets(invite),
            ["203.0.113.7:8090", "[2001:db8::1]:8091", f"{onion}:8092"],
        )
        self.assertEqual(pi.peer_id_of(invite), KEY.hex())

    def test_address_data_formats(self):
        self.assertEqual(
            pi.address_to_data("ip4", "1.2.3.4:258"), bytes([1, 2, 3, 4, 1, 2])
        )
        self.assertEqual(pi.data_to_address("ip4", bytes([1, 2, 3, 4, 1, 2])), "1.2.3.4:258")
        self.assertEqual(len(pi.address_to_data("ip6", "[::1]:1")), 18)
        self.assertEqual(
            pi.address_to_data("tor3", f"{pi._onion_text(ONION_KEY)}:9"),
            ONION_KEY + bytes([0, 9]),
        )

    def test_unknown_tag_is_ignored_and_the_rest_kept(self):
        raw = invite_pb2.PeerInvite(
            public_key=KEY,
            addr=[
                invite_pb2.Addr(tags=["quic9"], data=b"anything"),
                invite_pb2.Addr(tags=["quic9", "ip4"], data=pi.address_to_data("ip4", "1.2.3.4:5")),
            ],
        ).SerializeToString()
        invite = pi.parse_invite(raw)
        self.assertEqual(pi.invite_targets(invite), ["1.2.3.4:5"])

    def test_malformed_data_for_a_known_tag_is_refused(self):
        for tag, data in (("ip4", b"\x01\x02\x03\x04"), ("ip6", b"\x00" * 17), ("tor3", b"\x00" * 33)):
            raw = invite_pb2.PeerInvite(
                public_key=KEY, addr=[invite_pb2.Addr(tags=[tag], data=data)]
            ).SerializeToString()
            with self.assertRaises(pi.InviteError, msg=tag):
                pi.parse_invite(raw)

    def test_malformed_invitations_are_refused(self):
        with self.assertRaises(pi.InviteError):
            pi.parse_invite(b"\xff\xff\xff")
        with self.assertRaises(pi.InviteError):
            pi.parse_invite(invite_pb2.PeerInvite(public_key=b"short").SerializeToString())
        bad_formal = invite_pb2.PeerInvite(
            public_key=KEY, addr=[invite_pb2.Addr(tags=["ip4"], formal=b"no-equals", data=bytes(6))]
        ).SerializeToString()
        with self.assertRaises(pi.InviteError):
            pi.parse_invite(bad_formal)

    def test_size_of_one_ip4_address(self):
        size = len(pi.build_invite(_peer(("203.0.113.7", 8090))))
        self.assertLess(size, 60, size)

    def test_size_of_three_addresses(self):
        peer = _peer(("203.0.113.7", 8090), ("2001:db8::1", 8091), (pi._onion_text(ONION_KEY), 8092))
        size = len(pi.build_invite(peer))
        self.assertLess(size, 150, size)

    def test_hostnames_are_left_out(self):
        invite = pi.parse_invite(pi.build_invite(_peer(("node.example.org", 1), ("1.2.3.4", 2))))
        self.assertEqual(pi.invite_targets(invite), ["1.2.3.4:2"])

    def test_field_numbers_are_pinned(self):
        fields = lambda m: {f.name: f.number for f in m.DESCRIPTOR.fields}
        self.assertEqual(fields(invite_pb2.Addr), {"tags": 1, "formal": 2, "data": 3})
        self.assertEqual(fields(invite_pb2.PeerInvite), {"public_key": 1, "addr": 2})

    def test_qr_uses_byte_mode_at_level_m(self):
        qr = pi.qr_for_invite(pi.build_invite(_peer(("203.0.113.7", 8090))))
        self.assertEqual(qr.mode, "byte")
        self.assertEqual(qr.error, "M")

    def test_qr_refuses_over_1000_bytes_and_names_the_addr_to_remove(self):
        invite = invite_pb2.PeerInvite(public_key=KEY)
        for i in range(100):
            invite.addr.add(tags=["ip4"], data=pi.address_to_data("ip4", f"10.0.{i}.1:80"))
        with self.assertRaises(pi.InviteTooLarge) as caught:
            pi.qr_for_invite(invite.SerializeToString())
        self.assertIn("Remove the Addr entries #", str(caught.exception))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConnectInviteTests(unittest.TestCase):
    def _invite_file(self, *addresses):
        raw = pi.build_invite(_peer(*addresses))
        handle, path = tempfile.mkstemp()
        with os.fdopen(handle, "wb") as f:
            f.write(raw)
        self.addCleanup(os.unlink, path)
        return path

    def test_a_different_key_than_the_invited_one_is_refused(self):
        from src.commands import connect as connect_module

        channel = mock.Mock()
        with mock.patch.object(connect_module, "sc", mock.Mock()), \
                mock.patch.object(connect_module, "channel_and_peer_id", return_value=(channel, "ab" * 32)), \
                mock.patch.object(connect_module.BeeClient, "get_peer_info") as get_peer_info, \
                mock.patch.object(connect_module, "add_peer_instance") as add_peer_instance:
            added = connect_module.connect("1.2.3.4:5", expected_peer_id=KEY.hex())
        self.assertFalse(added)
        get_peer_info.assert_not_called()
        add_peer_instance.assert_not_called()
        channel.close.assert_called_once()

    def test_addresses_are_tried_in_order_with_the_invited_key(self):
        from src.commands import connect as connect_module

        path = self._invite_file(("1.2.3.4", 1), ("5.6.7.8", 2))
        with mock.patch.object(connect_module, "connect", side_effect=[False, True]) as connect:
            self.assertTrue(connect_module.connect_invite(path))
        self.assertEqual(
            [c.args[0] for c in connect.call_args_list], ["1.2.3.4:1", "5.6.7.8:2"]
        )
        for call in connect.call_args_list:
            self.assertEqual(call.kwargs["expected_peer_id"], KEY.hex())

    def test_an_invitation_without_a_known_address_does_not_connect(self):
        from src.commands import connect as connect_module

        raw = invite_pb2.PeerInvite(
            public_key=KEY, addr=[invite_pb2.Addr(tags=["quic9"], data=b"x")]
        ).SerializeToString()
        handle, path = tempfile.mkstemp()
        with os.fdopen(handle, "wb") as f:
            f.write(raw)
        self.addCleanup(os.unlink, path)
        with mock.patch.object(connect_module, "connect") as connect:
            self.assertFalse(connect_module.connect_invite(path))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
