"""Who a reputation event is written against.

Reputation is only useful if a score means "this counterparty failed me". Two
call sites broke that meaning in opposite directions, and both were invisible:

* `update_vmachine_reputation` moved the *hosting peer's* score by whatever a
  local instance had just done -- ran out of balance, lost its virtual machine.
  It reached the peer by splitting `id##peer_id` out of the vmachine id, a token
  scheme nothing mints any more, so in practice the event went nowhere at all.
* the failed-`Payable` penalty, which really is the peer's doing, was handed to
  `update_vmachine_reputation` with a peer id in the vmachine argument -- so it
  also went nowhere.

Neither shows up as an error: both functions return None and swallow the case.
The only way to catch it is to assert on which of the two is called.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system import payment_process
    from src.reputation_system import interface as reputation_interface
    from src.reputation_system.reasons import Reason
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VmachineOutcomesTests(unittest.TestCase):
    """What one of our own instances does is not evidence about a peer."""

    def test_a_vmachine_id_carrying_a_peer_id_moves_no_peer_score(self):
        with mock.patch.object(reputation_interface, "sc") as sc:
            sc.get_service_id_by_container_id.return_value = "service-1"
            reputation_interface.update_vmachine_reputation(
                vmachine_id="instance-1##peer-9", amount=-10,
                reason=Reason.INSTANCE_OUT_OF_BALANCE,
            )

        sc.update_reputation_peer.assert_not_called()
        sc.peer_exists.assert_not_called()
        # The service the instance ran is what carries the outcome.
        sc.update_reputation_service.assert_called_once_with(
            "service-1", -10, Reason.INSTANCE_OUT_OF_BALANCE
        )

    def test_an_instance_already_gone_scores_nothing_and_raises_nothing(self):
        """The pruning paths call this while tearing the instance down."""
        with mock.patch.object(reputation_interface, "sc") as sc:
            sc.get_service_id_by_container_id.side_effect = Exception("no such instance")
            scored = reputation_interface.update_vmachine_reputation(
                vmachine_id="instance-1", amount=-100, reason=Reason.INSTANCE_LOST
            )

        self.assertFalse(scored)
        sc.update_reputation_service.assert_not_called()

    def test_a_peer_failure_still_reaches_the_peer(self):
        """The direct path is untouched: this is a routing fix, not a removal."""
        with mock.patch.object(reputation_interface, "sc") as sc:
            sc.peer_exists.return_value = True
            reputation_interface.update_peer_reputation(
                peer_id="peer-9", amount=-100, reason=Reason.PEER_REFRESH_FAILED
            )

        sc.update_reputation_peer.assert_called_once_with(
            "peer-9", -100, Reason.PEER_REFRESH_FAILED
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PaymentCommunicationTests(unittest.TestCase):
    """A peer that will not take our payment call is the peer failing us."""

    def test_a_refused_payable_call_penalises_the_peer(self):
        attempt_payment_communication = getattr(
            payment_process, "__attempt_payment_communication"
        )
        reputation = mock.MagicMock()

        with mock.patch.object(payment_process, "__get_grpc_stub",
                               return_value=mock.MagicMock(), create=True), \
                mock.patch.object(payment_process, "bee") as bee, \
                mock.patch.object(payment_process, "COMMUNICATION_ATTEMPTS", 1), \
                mock.patch.object(payment_process, "_reputation_interface",
                                  return_value=reputation):
            bee.client_grpc.side_effect = Exception("peer refused the payment call")
            communicated = attempt_payment_communication(
                peer_id="peer-1",
                amount=1000,
                deposit_token="deposit-token-1",
                contract_ledger=celaut_pb2.Contract(),
            )

        self.assertFalse(communicated)
        reputation.update_peer_reputation.assert_called_once_with(
            peer_id="peer-1", amount=-1, reason=Reason.PAYMENT_CALL_FAILED
        )
        reputation.update_vmachine_reputation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
