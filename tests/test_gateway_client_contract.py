"""Callers and gateway agree on who has to present a Client, and say so when one is missing.

Every RPC the gateway gates (``client_gate.parse_with_client``) reads its payload at
index 1 and the Client at index 2. These pin the places that disagreed with that:
an outbound Payable that never sent a Client, a refresh asking GetPeerInfo with no
client_id, and a StartService that answered a caller without one with an empty stream
instead of an error.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.manager import manager
    from src.utils.bee_client import BeeClient
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

START_SERVICE_IMPORT_ERROR = None
try:
    from src.gateway.client_gate import ClientRequired
    from src.gateway.iterables import start_service_iterable
except Exception as import_exc:  # pragma: no cover - environment-dependent
    START_SERVICE_IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OutboundCallsCarryAClientTests(unittest.TestCase):
    def test_payable_sends_the_client_at_index_two(self):
        payment = celaut_pb2.Payment(deposit_token="token")
        with mock.patch.object(BeeClient, "call_one") as call_one:
            BeeClient.payable(mock.MagicMock(), payment, client_id="c" * 32)
        kwargs = call_one.call_args.kwargs
        self.assertEqual(
            kwargs["indices_serializer"],
            {1: celaut_pb2.Payment, 2: celaut_pb2.Client},
        )
        self.assertEqual(
            kwargs["input"], [payment, celaut_pb2.Client(client_id="c" * 32)]
        )

    def test_a_refresh_reuses_the_client_held_at_that_peer(self):
        with mock.patch.object(manager.sc, "get_peer_client", return_value="c" * 32), \
                mock.patch.object(manager, "_mint_client_over_channel") as mint, \
                mock.patch.object(BeeClient, "get_peer_info") as get_peer_info:
            manager.fetch_peer_info(mock.sentinel.channel, "peer")
        mint.assert_not_called()
        get_peer_info.assert_called_once_with(mock.sentinel.channel, client_id="c" * 32)

    def test_a_refresh_mints_and_stores_a_client_when_it_holds_none(self):
        with mock.patch.object(manager.sc, "get_peer_client", return_value=None), \
                mock.patch.object(manager, "_mint_client_over_channel",
                                  return_value="d" * 32), \
                mock.patch.object(manager.sc, "add_external_client") as store, \
                mock.patch.object(BeeClient, "get_peer_info") as get_peer_info:
            manager.fetch_peer_info(mock.sentinel.channel, "peer")
        store.assert_called_once_with(peer_id="peer", client_id="d" * 32)
        get_peer_info.assert_called_once_with(mock.sentinel.channel, client_id="d" * 32)


@unittest.skipIf(
    START_SERVICE_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {START_SERVICE_IMPORT_ERROR}",
)
class StartServiceWithoutAClientTests(unittest.TestCase):
    def _iterable(self, *, generated=False):
        iterable = start_service_iterable.StartServiceIterable.__new__(
            start_service_iterable.StartServiceIterable
        )
        iterable.context = mock.MagicMock()
        iterable.client_id = None
        iterable.service_hash = "a" * 64
        iterable.service_saved = True
        iterable.generated = generated
        return iterable

    def test_a_stream_ending_without_a_client_is_refused(self):
        with mock.patch.object(
            start_service_iterable, "require_caller", side_effect=ClientRequired("no")
        ):
            with self.assertRaises(ClientRequired):
                self._iterable().final()

    def test_a_served_request_is_not_checked_again(self):
        with mock.patch.object(start_service_iterable, "require_caller") as require:
            self._iterable(generated=True).final()
        require.assert_not_called()


if __name__ == "__main__":
    unittest.main()
