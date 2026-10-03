"""Every ledger is declared whole, and the same declaration is used everywhere it appears.

A ``Contract.Ledger`` names a chain, and two nodes settling on it have to agree on far
more than a tag: the network, the units, what each contract attribute holds, how a
payment is bound to a deposit token and -- on Ergo -- how a reputation proof box is
laid out and how its owner attests a peer. These pin that all of it is in ``formal``,
that a payment contract and a reputation proof carry the very same descriptor, and that
a peer declaring something else is refused rather than paid.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.identity.node_identity import attestation_payload, parse_component_formal
    from src.utils import ledger_descriptors
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

MANAGER_IMPORT_ERROR = None
try:
    from src.manager import manager
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    MANAGER_IMPORT_ERROR = import_exc


def _formal(ledger):
    return parse_component_formal(ledger.formal)


NETWORK_KEYS = ("chain", "network", "consensus", "asset.native", "asset.native.base_unit")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DescriptorTests(unittest.TestCase):
    def test_the_ergo_network_is_the_same_in_both_places(self):
        # The definition of the ledger itself repeats, whole, wherever it appears.
        payment = _formal(ledger_descriptors.ergo_payment_ledger())
        reputation = _formal(ledger_descriptors.ergo_reputation_ledger())
        network = {k: v for k, v in payment.items() if not k.startswith("payment.")}
        self.assertEqual(
            network, {k: v for k, v in reputation.items() if not k.startswith("reputation.")}
        )
        for key in NETWORK_KEYS:
            self.assertIn(key, network)
        self.assertEqual(network["network"], "mainnet")

    def test_each_place_carries_only_its_own_rules(self):
        payment = _formal(ledger_descriptors.ergo_payment_ledger())
        reputation = _formal(ledger_descriptors.ergo_reputation_ledger())
        self.assertFalse(any(k.startswith("reputation.") for k in payment))
        self.assertFalse(any(k.startswith("payment.") for k in reputation))
        self.assertIn("R4", payment["payment.deposit_token"])
        self.assertIn("reputation.contract.ergo_tree", reputation)
        self.assertEqual(
            reputation["reputation.attestation.payload"],
            attestation_payload("") + "<peer_id>",
        )

    def test_bitcoin_declares_how_a_payment_is_bound(self):
        formal = _formal(ledger_descriptors.bitcoin_payment_ledger())
        self.assertEqual(formal["network"], "mainnet")
        self.assertIn("OP_RETURN", formal["payment.deposit_token"])
        self.assertEqual(formal["payment.contract_type.p2wpkh"], "p2wpkh")

    def test_the_rate_unit_is_the_base_unit(self):
        # ContractRate.mu_per_unit is MU per nanoERG / satoshi / token base unit; the
        # whole unit is only how an amount is shown to a person.
        for ledger in ledger_descriptors.payment_ledgers():
            self.assertIn("base unit", _formal(ledger)["payment.rate.unit"].replace(
                "per satoshi", "per base unit"))

    def test_what_each_node_chooses_is_not_declared(self):
        # A wallet's derivation path, a deposit token's lifetime and a proof's total
        # supply are each node's own.
        for ledger in ledger_descriptors.payment_ledgers() + ledger_descriptors.reputation_ledgers():
            text = bytes(ledger.formal).decode()
            self.assertNotIn("m/44'", text)
            self.assertNotIn("ttl", text.lower())
            self.assertNotIn("total", text.lower())

    def test_formal_is_canonical(self):
        for ledger in ledger_descriptors.payment_ledgers() + ledger_descriptors.reputation_ledgers():
            keys = [l.split("=", 1)[0] for l in bytes(ledger.formal).decode().splitlines()]
            self.assertEqual(keys, sorted(keys))

    def test_payments_and_proofs_use_their_own_declaration(self):
        from src.payment_system.contracts.ergo import interface as ergo_payments
        from src.reputation_system.envs import ergo_ledger as reputation_ledger

        self.assertEqual(ergo_payments.ledger(), ledger_descriptors.ergo_payment_ledger())
        self.assertEqual(reputation_ledger(), ledger_descriptors.ergo_reputation_ledger())


@unittest.skipIf(
    IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or MANAGER_IMPORT_ERROR}",
)
class ForeignLedgerTests(unittest.TestCase):
    def _contract(self, ledger):
        return celaut_pb2.Contract(ledger=ledger)

    def test_a_contract_on_our_ledger_is_stored(self):
        self.assertTrue(
            manager._accept_contract(self._contract(ledger_descriptors.ergo_payment_ledger()), "p")
        )

    def test_a_contract_tagged_ergo_but_declaring_another_network_is_not(self):
        ledger = ledger_descriptors.ergo_payment_ledger()
        ledger.formal = bytes(ledger.formal).replace(b"network=mainnet", b"network=testnet")
        self.assertFalse(manager._accept_contract(self._contract(ledger), "p"))

    def test_a_ledger_declaring_only_its_tag_is_not_stored(self):
        # A node from before ledgers were declared: its rate is per whole unit, and
        # read here as per base unit it would be wrong by 10^9 (#467).
        for tag in ("ergo", "bitcoin"):
            legacy = celaut_pb2.Contract.Ledger(tags=[tag], formal=b"")
            self.assertFalse(manager._accept_contract(self._contract(legacy), "p"))
            self.assertIsNone(payment_process._ledger_tag(legacy))

    def test_a_proof_ledger_declaring_only_its_tag_does_not_match(self):
        from src.identity.node_identity import same_component, same_declaration

        legacy = celaut_pb2.Contract.Ledger(tags=["ergo"], formal=b"")
        ours = ledger_descriptors.reputation_ledger("ergo")
        self.assertTrue(same_component(legacy, ours))  # the loose rule, kept elsewhere
        self.assertFalse(same_declaration(legacy, ours))
        self.assertFalse(same_declaration(legacy, legacy))  # two empty formals agree on nothing

    def test_a_reputation_proof_declaring_only_its_tag_is_refused(self):
        from src.reputation_system.contracts.ergo import proof_validation
        from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE
        from src.utils.contract_xattrs import set_script

        def proof(ledger):
            contract = celaut_pb2.Contract(ledger=ledger)
            set_script(contract, bytes.fromhex(REPUTATION_PROOF_ERGO_TREE))
            return contract

        legacy = proof_validation.explain_contract_ledger(
            proof(celaut_pb2.Contract.Ledger(tags=["ergo"], formal=b"")), "00"
        )
        self.assertEqual(legacy, "Contract ledger not compatible: ledger=False script=True")
        declared = proof_validation.explain_contract_ledger(
            proof(ledger_descriptors.reputation_ledger("ergo")), "00"
        )
        self.assertNotIn("ledger=False", declared or "")

    def test_a_ledger_this_node_does_not_know_is_not_stored(self):
        ledger = celaut_pb2.Contract.Ledger(tags=["dogecoin"], formal=b"chain=dogecoin")
        self.assertFalse(manager._accept_contract(self._contract(ledger), "p"))

    def test_an_incoming_payment_on_another_ledger_has_no_tag(self):
        ledger = ledger_descriptors.bitcoin_payment_ledger()
        self.assertEqual(payment_process._ledger_tag(ledger), "bitcoin")
        ledger.formal = bytes(ledger.formal).replace(b"satoshi", b"millisatoshi")
        self.assertIsNone(payment_process._ledger_tag(ledger))


def _configured(**overrides):
    """``ledger_descriptors`` reading a config with ``overrides`` on top of this one's."""
    real = ledger_descriptors.ConfigManager

    class _Config:
        def get(self, key, default=None):
            if key in overrides:
                return overrides[key]
            return real().get(key, default)

    return mock.patch.object(ledger_descriptors, "ConfigManager", _Config)


@unittest.skipIf(
    IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or MANAGER_IMPORT_ERROR}",
)
class NetworkFromConfigTests(unittest.TestCase):
    """The network a descriptor declares is the one this node is configured on (#467)."""

    def test_a_signet_node_declares_signet_and_a_mainnet_node_does_not_store_it(self):
        with _configured(**{"ledgers.bitcoin.NETWORK": "signet"}):
            signet = ledger_descriptors.bitcoin_payment_ledger()
        mainnet = ledger_descriptors.bitcoin_payment_ledger()
        self.assertEqual(_formal(signet)["network"], "signet")
        self.assertEqual(_formal(mainnet)["network"], "mainnet")
        self.assertNotEqual(signet.formal, mainnet.formal)
        self.assertIn("Bitcoin signet", signet.prose)
        self.assertFalse(manager._accept_contract(celaut_pb2.Contract(ledger=signet), "p"))
        self.assertIsNone(payment_process._ledger_tag(signet))

    def test_a_node_on_another_ergo_chain_declares_its_genesis_block(self):
        testnet_genesis = "AB" * 32
        with _configured(**{"ledgers.ergo.GENESIS_BLOCK_ID": testnet_genesis}):
            payment = ledger_descriptors.ergo_payment_ledger()
            reputation = ledger_descriptors.ergo_reputation_ledger()
        self.assertEqual(_formal(payment)["network"], f"genesis {testnet_genesis.lower()}")
        self.assertEqual(_formal(reputation)["network"], _formal(payment)["network"])
        self.assertNotEqual(payment.formal, ledger_descriptors.ergo_payment_ledger().formal)
        self.assertFalse(manager._accept_contract(celaut_pb2.Contract(ledger=payment), "p"))

    def test_mainnet_genesis_is_named_mainnet(self):
        from src.reputation_system.contracts.ergo.utils import MAINNET_GENESIS_BLOCK_ID

        with _configured(**{"ledgers.ergo.GENESIS_BLOCK_ID": MAINNET_GENESIS_BLOCK_ID.upper()}):
            self.assertEqual(_formal(ledger_descriptors.ergo_payment_ledger())["network"], "mainnet")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProtocolCommandLedgerTests(unittest.TestCase):
    def test_our_declaration_lists_every_ledger_per_place(self):
        from src.commands import protocol

        declared = protocol.own_declaration(prose=False)
        self.assertEqual([d["tags"] for d in declared["payment_ledgers"]], [["ergo"], ["bitcoin"]])
        self.assertEqual([d["tags"] for d in declared["reputation_ledgers"]], [["ergo"]])

    def test_a_peer_ledger_that_differs_is_reported_with_the_key(self):
        from src.commands import protocol

        peer = celaut_pb2.Peer()
        rate = peer.payment_contracts.add()
        rate.contract.ledger.CopyFrom(ledger_descriptors.ergo_payment_ledger())
        rate.contract.ledger.formal = bytes(rate.contract.ledger.formal).replace(
            b"network=mainnet", b"network=testnet"
        )
        peer.reputation_proofs.add().ledger.CopyFrom(ledger_descriptors.ergo_reputation_ledger())
        # A proof carrying the payment declaration is not a proof's declaration.
        peer.reputation_proofs.add().ledger.CopyFrom(ledger_descriptors.ergo_payment_ledger())
        report = protocol._compare_ledgers(peer)
        self.assertEqual([r["status"] for r in report], ["differs", "match", "differs"])
        self.assertEqual(list(report[0]["formal_difference"]), ["network"])


if __name__ == "__main__":
    unittest.main()
