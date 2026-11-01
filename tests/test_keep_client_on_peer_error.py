"""A node forgets its client on a peer only when the peer says it does not know it (#602).

``_fetch_balance_on_peer`` used to clear ``remote_client_id`` on any error but a
transport failure. That id is the only credential for the deposit on the peer and the
father of every child delegated there, so UNIMPLEMENTED (a peer without
``GetClientMetrics``), the rate-limit quarantine or a server fault must leave it alone.
"""
import unittest
from unittest.mock import patch

import grpc

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.manager import metrics as metrics_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    metrics_mod = None  # type: ignore[assignment]


class _RpcError(grpc.RpcError):
    def __init__(self, code, details):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


UNKNOWN_CLIENT = _RpcError(
    grpc.StatusCode.UNKNOWN,
    "Exception calling application: This RPC requires a client_id minted by "
    "GenerateClient first. Wrong or missing client_id: 'abc'",
)
QUARANTINED = _RpcError(
    grpc.StatusCode.UNKNOWN,
    "Exception calling application: Client abc is calling too fast and is "
    "temporarily quarantined.",
)
UNIMPLEMENTED = _RpcError(grpc.StatusCode.UNIMPLEMENTED, "Method not found!")
INTERNAL = _RpcError(grpc.StatusCode.INTERNAL, "database is locked")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KeepClientOnPeerErrorTests(unittest.TestCase):
    def _fetch(self, error, delegated=()):
        with patch.object(
            metrics_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(metrics_mod, "peer_channel"), patch.object(
            metrics_mod.BeeClient, "get_client_metrics", side_effect=error
        ), patch.object(
            metrics_mod, "is_peer_available", return_value=True
        ), patch.object(
            metrics_mod.sc, "get_delegated_instances", return_value=list(delegated)
        ), patch.object(metrics_mod.sc, "delete_external_client") as delete:
            self.assertEqual(metrics_mod._fetch_balance_on_peer("peer-a"), 0)
        return delete

    def test_unimplemented_keeps_the_client(self):
        self._fetch(UNIMPLEMENTED).assert_not_called()

    def test_quarantine_keeps_the_client(self):
        self._fetch(QUARANTINED).assert_not_called()

    def test_internal_error_keeps_the_client(self):
        self._fetch(INTERNAL).assert_not_called()

    def test_unknown_client_forgets_it(self):
        self._fetch(UNKNOWN_CLIENT).assert_called_once_with(peer_id="peer-a")

    def test_unknown_client_is_kept_while_children_are_delegated_there(self):
        self._fetch(UNKNOWN_CLIENT, delegated=[{"peer_id": "peer-a"}]).assert_not_called()

    def test_children_on_another_peer_do_not_hold_it(self):
        self._fetch(UNKNOWN_CLIENT, delegated=[{"peer_id": "peer-b"}]).assert_called_once()


if __name__ == "__main__":
    unittest.main()
