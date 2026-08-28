"""The global FORWARD rule that lets return traffic through.

It has to be the *first* rule in the chain: everything below it is per-VM policy,
and traffic returning on an already-allowed connection must never be judged
against that. The ordering itself is enforced (and tested) by the backend's
``ensure_first``; what this file pins is that the virtualizer layer asks for the
right rule and reports failure honestly instead of pretending it succeeded.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.utils.firewall.errors import FirewallError
    from src.utils.firewall.rules import Chain, Verdict
    from protos import celaut_pb2 as celaut
    from src.virtualizers import firewall as vm_firewall
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    vm_firewall = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtualizerFirewallGlobalRuleTests(unittest.TestCase):
    def test_asks_the_backend_to_put_the_return_traffic_accept_first(self):
        with patch("src.virtualizers.ch.firewall.backend") as backend, patch.object(
            vm_firewall, "ensure_bridge_forward_passthrough", return_value=True
        ):
            backend.return_value.ensure_first.return_value = True
            self.assertTrue(vm_firewall.ensure_forward_related_established_rule())

        rule = backend.return_value.ensure_first.call_args.args[0]
        self.assertIs(rule.chain, Chain.FORWARD)
        self.assertIs(rule.verdict, Verdict.ACCEPT)
        self.assertEqual(rule.ct_states, ("RELATED", "ESTABLISHED"))
        self.assertTrue(rule.at_head)
        # Global, not scoped to any VM.
        self.assertIsNone(rule.source)
        self.assertEqual(rule.comment, vm_firewall.FORWARD_RELATED_ESTABLISHED_COMMENT)

    def test_an_already_correct_chain_is_still_a_success(self):
        with patch("src.virtualizers.ch.firewall.backend") as backend, patch.object(
            vm_firewall, "ensure_bridge_forward_passthrough", return_value=True
        ):
            backend.return_value.ensure_first.return_value = False
            self.assertTrue(vm_firewall.ensure_forward_related_established_rule())

    def test_a_firewall_failure_is_reported_not_swallowed(self):
        with patch("src.virtualizers.ch.firewall.backend") as backend:
            backend.return_value.ensure_first.side_effect = FirewallError("no permission")
            self.assertFalse(vm_firewall.ensure_forward_related_established_rule())

    def test_an_unexpected_failure_is_reported_too(self):
        with patch("src.virtualizers.ch.firewall.backend") as backend:
            backend.return_value.ensure_first.side_effect = RuntimeError("nft went missing")
            self.assertFalse(vm_firewall.ensure_forward_related_established_rule())

    def test_the_per_vm_entry_points_refuse_to_run_without_it(self):
        # If return traffic is not accepted first, applying per-VM policy would
        # half-configure the guest, so every entry point bails out instead.
        with patch.object(
            vm_firewall, "ensure_forward_related_established_rule", return_value=False
        ):
            self.assertFalse(vm_firewall.block_all(vmachine_id="vm-1"))
            self.assertFalse(
                vm_firewall.allow_connection_to_instance(
                    vmachine_id="vm-1", instance=celaut.Instance()
                )
            )


if __name__ == "__main__":
    unittest.main()
