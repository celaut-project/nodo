"""The payment orchestrator must call each ledger's process_payment as declared.

It passed ``token=``, while both implementations (Ergo and the simulator) declare
``deposit_token``. The call sits inside a broad `except Exception`, so the
TypeError surfaced as a payment failure rather than as a programming error:

    Error processing payment for contract 1c691f72…:
        process_payment() got an unexpected keyword argument 'token'

Nothing static catches this: the callable is looked up at runtime from the
payment-envs registry.
"""
import inspect
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system import payment_process
    from src.payment_system.contracts.ergo import interface as ergo_interface
    from src.payment_system.contracts.simulator import interface as simulator_interface
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]

# What __peer_payment_process passes to the ledger implementation.
EXPECTED_ARGUMENTS = {"amount", "deposit_token", "ledger", "script"}

CONTRACT_HASH = "1c691f72aad8533f1e0815cb6dd9f302637d5c60824c8a92684fe50cdd4b82bd"
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)


class _Envs:
    """Minimal stand-in for the payment-envs registry."""

    DEMOS = ()

    def __init__(self, implementation):
        self._implementation = implementation

    def available_payment_process(self):
        return {CONTRACT_HASH: self._implementation}

    def check_sender_balances(self):
        return {CONTRACT_HASH: lambda amount: True}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProcessPaymentSignatureTests(unittest.TestCase):
    def test_ergo_declares_the_expected_arguments(self):
        params = set(inspect.signature(ergo_interface.process_payment).parameters)
        self.assertEqual(params, EXPECTED_ARGUMENTS)

    def test_the_simulator_declares_the_expected_arguments(self):
        params = set(inspect.signature(simulator_interface.process_payment).parameters)
        self.assertEqual(params, EXPECTED_ARGUMENTS)

    def test_the_orchestrator_calls_the_implementation_it_was_given(self):
        # Drive the real orchestrator with a stub implementation that only accepts
        # the declared names, so a renamed keyword fails here instead of at the
        # first real payment.
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")
        calls = []

        def implementation(amount, deposit_token, ledger, script):
            calls.append({"amount": amount, "deposit_token": deposit_token,
                          "ledger": ledger, "script": script})
            return celaut_pb2.Contract(ledger=ledger)

        peer_payment_process = getattr(payment_process, "__peer_payment_process")

        with mock.patch.object(payment_process, "_payment_envs", return_value=_Envs(implementation)), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  return_value=iter([(SCRIPT, ledger)])), \
                mock.patch.object(payment_process, "ledger_balancer",
                                  side_effect=lambda ledger_generator: ledger_generator), \
                mock.patch.object(payment_process, "__obtain_deposit_token",
                                  return_value="deposit-token-1", create=True):
            peer_payment_process(peer_id="peer-1", amount=1000)

        # Only the call into the ledger implementation is under test here; what the
        # orchestrator does afterwards (peer round-trips, gas bookkeeping) is not.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["deposit_token"], "deposit-token-1")
        self.assertEqual(calls[0]["amount"], 1000)
        self.assertEqual(calls[0]["script"], SCRIPT)


if __name__ == "__main__":
    unittest.main()
