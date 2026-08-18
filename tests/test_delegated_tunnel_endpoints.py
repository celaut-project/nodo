"""Tests for standing in locally for a service delegated to a peer.

Covers the policy (``network.DELEGATION_TUNNEL_POLICY``), the rewriting of the
``uri_slot`` handed to our client, and the endpoint lifecycle. The relay itself is
covered by ``test_service_tunnel*``; here the peer's gateway is a stub, so the
tests assert what our client is *told*, not what crosses the wire.
"""

import socket
import threading
import unittest
from typing import List, Optional
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.tunneling import delegated_endpoints
    from src.virtualizers.firewall import TransportProtocol
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    delegated_endpoints = None  # type: ignore[assignment]


def _peer_instance(port: int, transport: str = "tcp", ip: str = "10.9.9.9") -> "celaut.Instance":
    """What a peer answers with: its own address for a declared slot."""
    return celaut.Instance(
        api=celaut.Service.Api(
            slot=[
                celaut.Service.Api.Slot(
                    port=port,
                    transport=celaut.Service.Api.Protocol(tags=[transport]),
                )
            ]
        ),
        uri_slot=[
            celaut.Instance.Uri_Slot(
                internal_port=port,
                uri=[celaut.Instance.Uri(ip=ip, port=port)],
            )
        ],
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegationTunnelPolicyTests(unittest.TestCase):
    def _should_tunnel(
        self,
        policy: str,
        instance: "celaut.Instance",
        reachable: bool = True,
    ) -> bool:
        with patch.object(
            delegated_endpoints.env_manager, "get", side_effect=lambda key, default=None: (
                policy if key == "network.DELEGATION_TUNNEL_POLICY" else default
            )
        ), patch.object(delegated_endpoints.utils, "is_open", return_value=reachable):
            return delegated_endpoints.should_tunnel(instance)

    def test_always_tunnels_even_when_the_peer_answers(self):
        self.assertTrue(
            self._should_tunnel("always", _peer_instance(8080), reachable=True)
        )

    def test_never_hands_over_the_peer_address_even_when_unreachable(self):
        self.assertFalse(
            self._should_tunnel("never", _peer_instance(8080), reachable=False)
        )

    def test_auto_skips_the_tunnel_when_the_peer_is_reachable(self):
        """A client on the same network must not pay for a hop it does not need."""
        self.assertFalse(
            self._should_tunnel("auto", _peer_instance(8080), reachable=True)
        )

    def test_auto_tunnels_when_the_peer_does_not_answer(self):
        self.assertTrue(
            self._should_tunnel("auto", _peer_instance(8080), reachable=False)
        )

    def test_auto_tunnels_udp_because_it_cannot_be_probed(self):
        """UDP has no handshake, so 'reachable' is unknowable — assume it is not."""
        self.assertTrue(
            self._should_tunnel(
                "auto", _peer_instance(5353, transport="udp"), reachable=True
            )
        )

    def test_an_unknown_policy_falls_back_to_auto(self):
        self.assertTrue(
            self._should_tunnel("sometimes", _peer_instance(8080), reachable=False)
        )
        self.assertFalse(
            self._should_tunnel("sometimes", _peer_instance(8080), reachable=True)
        )

    def test_an_instance_advertising_nothing_is_not_tunnelled(self):
        self.assertFalse(self._should_tunnel("auto", celaut.Instance()))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedEndpointPublishTests(unittest.TestCase):
    TOKEN = "peer-side-token"

    def setUp(self):
        # A free port range confined to the ephemeral space keeps the test honest
        # about going through get_free_port instead of binding port 0.
        self.port_patch = patch.object(
            delegated_endpoints.env_manager,
            "get",
            side_effect=lambda key, default=None: (
                [{"START": 45000, "END": 45999}]
                if key == "network.FREE_PORTS_RANGE"
                else default
            ),
        )
        self.port_patch.start()

    def tearDown(self):
        delegated_endpoints.close(token=self.TOKEN)
        self.port_patch.stop()

    def test_uri_slot_is_rewritten_to_a_listening_local_endpoint(self):
        peer = _peer_instance(8080)

        rewritten = delegated_endpoints.publish(
            token=self.TOKEN,
            peer_gateway="10.9.9.9:8090",
            instance=peer,
            bind_ip="127.0.0.1",
        )

        self.assertEqual(len(rewritten.uri_slot), 1)
        slot = rewritten.uri_slot[0]
        # The slot the client asks for is unchanged; only where it connects moves.
        self.assertEqual(slot.internal_port, 8080)
        self.assertEqual(len(slot.uri), 1)
        self.assertEqual(slot.uri[0].ip, "127.0.0.1")
        self.assertNotEqual(slot.uri[0].port, 8080)
        self.assertTrue(45000 <= slot.uri[0].port <= 45999)
        # The API declaration is passed through untouched.
        self.assertEqual(rewritten.api, peer.api)

        # And something is really listening there.
        probe = socket.create_connection(("127.0.0.1", slot.uri[0].port), timeout=5)
        probe.close()

    def test_a_udp_slot_gets_a_udp_listener(self):
        rewritten = delegated_endpoints.publish(
            token=self.TOKEN,
            peer_gateway="10.9.9.9:8090",
            instance=_peer_instance(5353, transport="udp"),
            bind_ip="127.0.0.1",
        )

        local_port = rewritten.uri_slot[0].uri[0].port
        # A TCP connect must fail: the listener is a datagram socket.
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", local_port), timeout=1).close()

    def test_close_releases_the_local_port(self):
        rewritten = delegated_endpoints.publish(
            token=self.TOKEN,
            peer_gateway="10.9.9.9:8090",
            instance=_peer_instance(8080),
            bind_ip="127.0.0.1",
        )
        local_port = rewritten.uri_slot[0].uri[0].port

        delegated_endpoints.close(token=self.TOKEN)

        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", local_port), timeout=1).close()

    def test_restore_rebinds_the_same_port_the_client_was_given(self):
        """A client holding the old address cannot be told about a new port."""
        rewritten = delegated_endpoints.publish(
            token=self.TOKEN,
            peer_gateway="10.9.9.9:8090",
            instance=_peer_instance(8080),
            bind_ip="127.0.0.1",
        )
        pinned_port = rewritten.uri_slot[0].uri[0].port
        delegated_endpoints.close(token=self.TOKEN)

        again = delegated_endpoints.publish(
            token=self.TOKEN,
            peer_gateway="10.9.9.9:8090",
            instance=rewritten,
            bind_ip="127.0.0.1",
            port_by_slot={8080: pinned_port},
        )

        self.assertEqual(again.uri_slot[0].uri[0].port, pinned_port)
        probe = socket.create_connection(("127.0.0.1", pinned_port), timeout=5)
        probe.close()

    def test_a_slot_without_usable_transport_keeps_the_peer_address(self):
        """Never silently drop a slot: hand over the peer's address unchanged."""
        peer = celaut.Instance(
            api=celaut.Service.Api(
                slot=[celaut.Service.Api.Slot(port=8080)]  # no transport declared
            ),
            uri_slot=[
                celaut.Instance.Uri_Slot(
                    internal_port=8080,
                    uri=[celaut.Instance.Uri(ip="10.9.9.9", port=8080)],
                )
            ],
        )

        rewritten = delegated_endpoints.publish(
            token=self.TOKEN,
            peer_gateway="10.9.9.9:8090",
            instance=peer,
            bind_ip="127.0.0.1",
        )

        self.assertEqual(rewritten.uri_slot[0].uri[0].ip, "10.9.9.9")
        self.assertEqual(rewritten.uri_slot[0].uri[0].port, 8080)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OwnAddressDetectionTests(unittest.TestCase):
    """`restore` must recognise which stored instances were ours to serve."""

    def test_loopback_is_ours(self):
        self.assertTrue(delegated_endpoints._is_own_address("127.0.0.1"))

    def test_a_foreign_address_is_not_ours(self):
        # Documentation range, guaranteed not to be a local interface.
        self.assertFalse(delegated_endpoints._is_own_address("192.0.2.10"))

    def test_local_addresses_are_extracted_only_when_ours(self):
        ours = _peer_instance(8080, ip="127.0.0.1")
        ours.uri_slot[0].uri[0].port = 45123

        extracted = delegated_endpoints._local_addresses(ours)

        self.assertEqual(extracted, ("127.0.0.1", {8080: 45123}))
        self.assertIsNone(delegated_endpoints._local_addresses(_peer_instance(8080)))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RestoreTests(unittest.TestCase):
    """`restore()` reads `delegated_instances` directly; `publish()` is stubbed."""

    TOKEN = "peer-side-token"
    PEER_ID = "peer-1"

    def _stored_row(self, instance: "celaut.Instance") -> dict:
        return {
            'token': self.TOKEN,
            'id': 'hashed-alias',
            'peer_id': self.PEER_ID,
            'father_id': 'father-instance',
            'serialized_instance': instance.SerializeToString(),
        }

    def _tunnelled_instance(self, port: int = 8080, local_port: int = 45123) -> "celaut.Instance":
        """What gets stored after a successful publish(): our own address."""
        return celaut.Instance(
            api=celaut.Service.Api(
                slot=[celaut.Service.Api.Slot(
                    port=port, transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                )]
            ),
            uri_slot=[celaut.Instance.Uri_Slot(
                internal_port=port,
                uri=[celaut.Instance.Uri(ip="127.0.0.1", port=local_port)],
            )],
        )

    def test_a_never_tunnelled_instance_is_left_alone(self):
        """The peer's own address is not ours, so restore() has nothing to do."""
        stored = self._stored_row(_peer_instance(8080))

        with patch.object(delegated_endpoints.sc, "get_delegated_instances", return_value=[stored]), \
             patch.object(delegated_endpoints, "publish") as publish_mock:
            restored = delegated_endpoints.restore()

        self.assertEqual(restored, 0)
        publish_mock.assert_not_called()

    def test_a_successful_restore_rebinds_the_port_the_client_holds(self):
        stored = self._stored_row(self._tunnelled_instance())

        with patch.object(
            delegated_endpoints.sc, "get_delegated_instances", return_value=[stored]
        ), patch.object(
            delegated_endpoints.utils, "generate_uris_by_peer_id", return_value=iter(["10.9.9.9:8090"])
        ), patch.object(
            delegated_endpoints, "publish"
        ) as publish_mock, patch.object(
            delegated_endpoints, "endpoint_count", return_value=1
        ):
            restored = delegated_endpoints.restore()

        self.assertEqual(restored, 1)
        publish_mock.assert_called_once()
        # The pinned port matters more than anything else here: a client holding
        # the old address cannot be told about a new one.
        self.assertEqual(publish_mock.call_args.kwargs["port_by_slot"], {8080: 45123})
        self.assertEqual(publish_mock.call_args.kwargs["bind_ip"], "127.0.0.1")

    def test_a_port_that_cannot_be_rebound_is_not_counted_as_restored(self):
        """The real failure mode: something took the port while the node was down.

        Nothing comes up, the client's address leads nowhere, and the count must
        say so instead of reporting a restore that did not happen.
        """
        stored = self._stored_row(self._tunnelled_instance())

        with patch.object(
            delegated_endpoints.sc, "get_delegated_instances", return_value=[stored]
        ), patch.object(
            delegated_endpoints.utils, "generate_uris_by_peer_id", return_value=iter(["10.9.9.9:8090"])
        ), patch.object(
            delegated_endpoints, "_open_endpoint", return_value=None  # bind fails
        ):
            restored = delegated_endpoints.restore()

        self.assertEqual(restored, 0)
        self.assertEqual(delegated_endpoints.endpoint_count(self.TOKEN), 0)

    def test_the_stored_record_is_never_rewritten_by_a_restore(self):
        """It holds the port the client was given: the only thing that makes a
        later recovery possible once that port frees up."""
        stored = self._stored_row(self._tunnelled_instance())
        before = stored['serialized_instance']

        with patch.object(
            delegated_endpoints.sc, "get_delegated_instances", return_value=[stored]
        ), patch.object(
            delegated_endpoints.utils, "generate_uris_by_peer_id", return_value=iter(["10.9.9.9:8090"])
        ), patch.object(
            delegated_endpoints, "_open_endpoint", return_value=None
        ):
            delegated_endpoints.restore()

        self.assertEqual(stored['serialized_instance'], before)
        # There is no update path at all: the accessor does not exist.
        self.assertFalse(hasattr(delegated_endpoints.sc, "update_delegated_instance"))

    def test_every_reopened_endpoint_is_counted_not_just_the_instance(self):
        """An instance with two slots that both come back up counts as two."""
        two_slots = celaut.Instance(
            api=celaut.Service.Api(slot=[
                celaut.Service.Api.Slot(
                    port=port, transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                ) for port in (8080, 8081)
            ]),
            uri_slot=[
                celaut.Instance.Uri_Slot(
                    internal_port=port,
                    uri=[celaut.Instance.Uri(ip="127.0.0.1", port=local)],
                ) for port, local in ((8080, 45123), (8081, 45124))
            ],
        )

        with patch.object(
            delegated_endpoints.sc, "get_delegated_instances",
            return_value=[self._stored_row(two_slots)]
        ), patch.object(
            delegated_endpoints.utils, "generate_uris_by_peer_id", return_value=iter(["10.9.9.9:8090"])
        ), patch.object(
            delegated_endpoints, "publish"
        ), patch.object(
            delegated_endpoints, "endpoint_count", return_value=2
        ):
            restored = delegated_endpoints.restore()

        self.assertEqual(restored, 2)

    def test_an_unreadable_stored_instance_is_skipped(self):
        stored = self._stored_row(_peer_instance(8080))
        stored['serialized_instance'] = b"not a valid protobuf message at all \xff\xfe"

        with patch.object(delegated_endpoints.sc, "get_delegated_instances", return_value=[stored]), \
             patch.object(delegated_endpoints, "publish") as publish_mock:
            restored = delegated_endpoints.restore()

        self.assertEqual(restored, 0)
        publish_mock.assert_not_called()

    def test_an_unreachable_peer_is_skipped(self):
        stored = self._stored_row(self._tunnelled_instance())

        with patch.object(
            delegated_endpoints.sc, "get_delegated_instances", return_value=[stored]
        ), patch.object(
            delegated_endpoints.utils, "generate_uris_by_peer_id", return_value=iter([])
        ), patch.object(
            delegated_endpoints, "publish"
        ) as publish_mock:
            restored = delegated_endpoints.restore()

        self.assertEqual(restored, 0)
        publish_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
