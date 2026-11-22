"""The offer that a node signs and keeps until ``valid_until`` (#657).

The node signs its offer (``EstimatedCost``) with a time. A caller that sends it back in
``Configuration.offer`` before that time gets the price of the offer, even if the load
moved, and the caller checks the quote of the start with no room. These tests use the real
signing path (an identity from a mnemonic) and the real pricing functions.
"""
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
# Imported here, not in a test: the first import of this module loads a configuration, and
# a configuration loaded while the suite runs changes the identity of the node for the tests
# that follow (the TLS certificate is cached for the process).
from src.gateway.launcher.delegate_execution import delegate_execution as delegation  # noqa: E402
from src.utils.cost_functions import execution_cost, offer_honor, price_bound, signed_offer  # noqa: E402
from src.utils.cost_functions import generate_estimated_cost as quote_module  # noqa: E402
from tests.reputation_system.test_offchain_reputation import _Signer  # noqa: E402
from tests.test_price_volatility import GIB, IDLE, _config  # noqa: E402

NOW = 1_000_000
BUSY = {"cpu": 0.9, "mem": 0.9, "disk": 0.9}


def _resources(mem=GIB) -> celaut.Service.Container.Resources:
    guest = celaut.Sysresources(mem_limit=mem, cpu_period=100000, cpu_quota=100000, disk_space=GIB)
    resources = celaut.Service.Container.Resources()
    resources.at_init.CopyFrom(guest)
    resources.at_most.CopyFrom(guest)
    return resources


def _offer(scarcity, resources=None, **config):
    """The offer of a node at ``scarcity``, as ``generate_estimated_cost`` builds it."""
    with _config(**config), \
            patch.object(quote_module, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
            patch.object(quote_module, "compute_start_service_cost", return_value=0), \
            patch.object(quote_module, "_service_hash_or_none", return_value=None):
        return quote_module.generate_estimated_cost(
            metadata=celaut.Metadata(), config=celaut.Configuration(),
            resources=resources or _resources(), check_availability=False, scarcity=scarcity)


def _honored(node, offer, resources=None, now=NOW):
    with node, _config(), \
            patch.object(quote_module, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
            patch.object(quote_module, "compute_start_service_cost", return_value=0), \
            patch.object(quote_module, "_service_hash_or_none", return_value=None):
        return offer_honor.honored(
            offer, metadata=celaut.Metadata(), config=celaut.Configuration(),
            resources=resources or _resources(), arch=None, service=None, now=now)


class TheOfferCarriesItsLoad(unittest.TestCase):
    def test_the_offer_has_the_quantised_load_it_is_priced_at(self):
        offer = _offer(BUSY)
        self.assertEqual(signed_offer.scarcity_of(offer),
                         {"cpu": 900000, "mem": 900000, "disk": 900000})

    def test_a_replay_at_the_load_of_the_offer_gives_the_same_figures(self):
        offer = _offer({"cpu": 0.1234567, "mem": 0.7654321, "disk": 0.5})
        again = _offer(signed_offer.to_scarcity(signed_offer.scarcity_of(offer)))
        self.assertEqual(offer.init_maintenance_cost, again.init_maintenance_cost)
        self.assertEqual(offer.max_maintenance_cost, again.max_maintenance_cost)

    def test_a_busier_node_offers_a_higher_price(self):
        self.assertGreater(int(_offer(BUSY).init_maintenance_cost.n),
                           int(_offer(IDLE).init_maintenance_cost.n))


class TheSignature(unittest.TestCase):
    def setUp(self):
        self.node = _Signer()
        self.offer = _offer(BUSY)

    def sealed(self, until=NOW + 60):
        offer = celaut.EstimatedCost()
        offer.CopyFrom(self.offer)
        with self.node:
            self.assertTrue(signed_offer.seal(offer, until))
        return offer

    def test_a_sealed_offer_verifies_with_the_key_of_the_node(self):
        offer = self.sealed()
        self.assertEqual(offer.valid_until, NOW + 60)
        self.assertTrue(signed_offer.verify(offer, self.node.peer_id))
        self.assertEqual(signed_offer.committed_until(offer, self.node.peer_id), NOW + 60)

    def test_no_field_can_change_after_the_signature(self):
        for field, change in (
                ("cost", lambda o: o.cost.__setattr__("n", "1")),
                ("init", lambda o: o.init_maintenance_cost.__setattr__("n", "1")),
                ("max", lambda o: o.max_maintenance_cost.__setattr__("n", "1")),
                ("loop", lambda o: setattr(o, "maintenance_seconds_loop", 1)),
                ("volatility", lambda o: setattr(o, "price_volatility", 0.5)),
                ("period", lambda o: setattr(o, "volatility_period_seconds", 1)),
                ("closes_at", lambda o: setattr(o, "closes_at", 1)),
                ("on_close", lambda o: setattr(o, "on_close", celaut.ON_CLOSE_STOP)),
                ("valid_until", lambda o: setattr(o, "valid_until", NOW + 3600)),
                ("scarcity", lambda o: o.launch_scarcity[0].__setattr__("value", 0)),
        ):
            with self.subTest(field):
                offer = self.sealed()
                change(offer)
                self.assertFalse(signed_offer.verify(offer, self.node.peer_id))

    def test_another_key_does_not_verify(self):
        self.assertFalse(signed_offer.verify(self.sealed(), _Signer().peer_id))

    def test_a_time_with_no_valid_signature_is_no_promise(self):
        offer = self.sealed()
        offer.signature = ""
        self.assertEqual(signed_offer.committed_until(offer, self.node.peer_id), 0)
        offer = self.sealed()
        offer.valid_until += 1
        self.assertEqual(signed_offer.committed_until(offer, self.node.peer_id), 0)

    def test_a_node_with_no_identity_makes_no_promise(self):
        offer = celaut.EstimatedCost()
        offer.CopyFrom(self.offer)
        with patch.object(signed_offer, "get_node_public_key_hex", return_value=None):
            self.assertFalse(signed_offer.seal(offer, NOW + 60))
        self.assertEqual((offer.valid_until, offer.signature, len(offer.launch_scarcity)), (0, "", 0))


class SigningTheOffer(unittest.TestCase):
    def test_the_validity_comes_from_the_setting(self):
        node = _Signer()
        with node, _config(**{"network__OFFER_VALIDITY_SECONDS": 30}):
            signed = offer_honor.sign(_offer(BUSY), now=NOW)
        self.assertEqual(signed.valid_until, NOW + 30)
        self.assertTrue(signed_offer.verify(signed, node.peer_id))

    def test_zero_makes_no_promise(self):
        with _Signer(), _config(**{"network__OFFER_VALIDITY_SECONDS": 0}):
            signed = offer_honor.sign(_offer(BUSY), now=NOW)
        self.assertEqual((signed.valid_until, signed.signature, len(signed.launch_scarcity)), (0, "", 0))

    def test_the_offer_given_is_not_changed(self):
        offer = _offer(BUSY)
        before = offer.SerializeToString()
        with _Signer(), _config():
            offer_honor.sign(offer, now=NOW)
        self.assertEqual(offer.SerializeToString(), before)


class KeepingThePrice(unittest.TestCase):
    """What the node does with the offer at the start (``offer_honor.honored``)."""

    def setUp(self):
        self.node = _Signer()
        with self.node, _config():
            self.offer = offer_honor.sign(_offer(BUSY), now=NOW)

    def test_a_valid_offer_prices_the_start_at_its_load_and_bound(self):
        scarcity, bound = _honored(self.node, self.offer)
        self.assertEqual(scarcity, {"cpu": 0.9, "mem": 0.9, "disk": 0.9})
        self.assertEqual(bound, price_bound.of_quote(self.offer))

    def test_the_price_of_the_offer_holds_when_the_load_moved(self):
        scarcity, _ = _honored(self.node, self.offer)   # enters `self.node` itself
        with _config():
            kept = execution_cost.launch_price_bp(scarcity)
            now_bp = execution_cost.launch_price_bp(IDLE)
        self.assertGreater(kept["mem"], now_bp["mem"])

    def test_no_offer_or_one_with_no_time_is_ignored(self):
        self.assertIsNone(_honored(self.node, None))
        self.assertIsNone(_honored(self.node, _offer(BUSY)))

    def test_a_past_offer_is_ignored(self):
        self.assertIsNone(_honored(self.node, self.offer, now=self.offer.valid_until + 1))
        self.assertIsNotNone(_honored(self.node, self.offer, now=self.offer.valid_until))

    def test_an_offer_that_this_node_did_not_sign_is_ignored(self):
        self.assertIsNone(_honored(_Signer(), self.offer))

    def test_an_offer_that_was_changed_is_ignored(self):
        self.offer.init_maintenance_cost.n = "1"
        self.assertIsNone(_honored(self.node, self.offer))

    def test_the_offer_of_a_small_service_does_not_start_a_large_one(self):
        # The signature is valid, and the figures are not those of this service.
        self.assertIsNone(_honored(self.node, self.offer, resources=_resources(mem=8 * GIB)))


class TheCallerChecksWithNoRoom(unittest.TestCase):
    OFFER = {"init_maintenance_cost": 100, "max_maintenance_cost": 200,
             "maintenance_seconds_loop": 10, "price_bounded": True, "price_volatility": 0.5,
             "volatility_period_seconds": 3600, "on_close": "refuse", "closes_at": 0}
    SIGNED = {"quote_init_maintenance_cost": 140, "quote_max_maintenance_cost": 200,
              "quote_maintenance_seconds_loop": 10, "quote_price_bounded": 1,
              "quote_price_volatility": 0.5, "quote_volatility_period_seconds": 3600,
              "quote_on_close": "refuse", "quote_closes_at": 0, "quote_launch_ts": NOW}

    def stricter(self, kept):
        return delegation.stricter_quote(self.OFFER, self.SIGNED, kept=kept)

    def test_without_a_commitment_the_room_is_the_volatility(self):
        columns, notes = self.stricter(False)
        self.assertEqual(columns["quote_init_maintenance_cost"], 140)  # under 100 * 1.5
        self.assertEqual(notes, [])

    def test_with_a_commitment_a_figure_above_the_offer_is_held_to_it(self):
        columns, notes = self.stricter(True)
        self.assertEqual(columns["quote_init_maintenance_cost"], 100)
        self.assertEqual(columns["quote_max_maintenance_cost"], 200)
        self.assertEqual(len(notes), 1)


class SendingTheOffer(unittest.TestCase):
    """``delegate_execution`` sends the offer of the peer, only when it is a commitment."""

    def setUp(self):
        self.peer = _Signer()
        with self.peer, _config():
            self.offer = offer_honor.sign(_offer(BUSY), now=NOW)

    def run_delegation(self, peer_quote, now=NOW):
        module = delegation
        sent = {}

        def start_service(channel, request, timeout=None):
            sent["config"] = request
            raise RuntimeError("stop here")

        config = celaut.Configuration()
        config.initial_mu.n = "1000"
        config.offer.valid_until = 7   # what a caller put there is never sent
        with patch.object(module.time, "time", return_value=now), \
                patch.object(module, "matching_payment_system"), \
                patch.object(module, "configuration_for_peer",
                             side_effect=lambda c, **k: _copy(c)), \
                patch.object(module, "balance_on_other_peer", return_value=10 ** 9), \
                patch.object(module, "get_client_id_on_other_peer", return_value="c"), \
                patch.object(module, "peer_channel"), \
                patch.object(module, "SQLConnection"), \
                patch.object(module.start_ref, "begin"), \
                patch.object(module.utils, "service_extended",
                             side_effect=lambda **kw: kw["config"]), \
                patch.object(module.BeeClient, "start_service", side_effect=start_service), \
                patch.object(module.statement_check, "unreachable", return_value=False):
            try:
                module.delegate_execution(
                    service_id="s", peer=self.peer.peer_id, father_id="f", metadata=celaut.Metadata(),
                    config=config, recursion_guard_token="t", refund_container=[lambda: None],
                    own_build_mu=1, peer_quote=peer_quote)
            except Exception:
                pass
        return sent["config"]

    def test_a_commitment_goes_with_the_start(self):
        sent = self.run_delegation(self.offer)
        self.assertEqual(sent.offer.SerializeToString(), self.offer.SerializeToString())

    def test_an_offer_with_no_promise_does_not_go(self):
        sent = self.run_delegation(_offer(BUSY))
        self.assertFalse(sent.HasField("offer"))

    def test_an_offer_about_to_end_does_not_go(self):
        sent = self.run_delegation(self.offer, now=self.offer.valid_until - 1)
        self.assertFalse(sent.HasField("offer"))

    def test_an_offer_of_another_node_does_not_go(self):
        other = celaut.EstimatedCost()
        other.CopyFrom(self.offer)
        other.signature = _signature_of(_Signer(), other)
        self.assertFalse(self.run_delegation(other).HasField("offer"))


def _copy(config):
    copy = celaut.Configuration()
    copy.CopyFrom(config)
    return copy


def _signature_of(node, offer):
    from src.identity.node_identity import offer_payload, sign_peer_payload
    with node:
        return sign_peer_payload(offer_payload(node.peer_id, signed_offer.offer_digest(offer)))


if __name__ == "__main__":
    unittest.main()
