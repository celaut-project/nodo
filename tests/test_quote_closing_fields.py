"""The quote carries `closes_at` and `on_close` from the configuration (issue #344)."""
import unittest
from datetime import datetime
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.payment_system import mu_conversion  # noqa: E402
from src.utils import activity_window, on_close  # noqa: E402
from src.utils.cost_functions import execution_cost, peer_quotes  # noqa: E402
from tests.test_price_bound import IDLE, _config, _guest  # noqa: E402


def _settings(**values):
    return patch.object(
        activity_window.env_manager, "get",
        side_effect=lambda key, default=None: values.get(key.split(".", 1)[1], default),
    )


def _quote(**window):
    from src.utils.cost_functions import generate_estimated_cost as module

    resources = celaut.Service.Container.Resources(at_init=_guest(), at_most=_guest())
    with _config(), _settings(**window), \
            patch.object(execution_cost, "system_scarcity", return_value=IDLE), \
            patch.object(module, "resolve_billable_resources", side_effect=lambda r, *a, **k: r), \
            patch.object(module, "compute_start_service_cost", return_value=0), \
            patch.object(module, "_service_hash_or_none", return_value=None):
        return module.generate_estimated_cost(
            metadata=celaut.Metadata(), config=celaut.Configuration(),
            resources=resources, check_availability=False)


class QuoteClosingFieldsTests(unittest.TestCase):
    def test_the_defaults_give_zero_and_refuse(self):
        quote = _quote()
        self.assertEqual(quote.closes_at, 0)
        self.assertEqual(quote.on_close, celaut.ON_CLOSE_REFUSE)

    def test_a_window_gives_its_next_close_and_its_policy(self):
        window = dict(ENABLED=True, WINDOWS=[{"START": "08:00", "END": "18:00"}],
                      ON_CLOSE="hibernate")
        quote = _quote(**window)
        with _settings(**window):
            expected = activity_window.next_close()
        self.assertGreater(quote.closes_at, 0)
        self.assertEqual(quote.closes_at, expected)
        self.assertEqual(quote.on_close, celaut.ON_CLOSE_HIBERNATE)

    def test_without_a_window_the_quote_says_refuse_whatever_on_close_holds(self):
        quote = _quote(ENABLED=False, ON_CLOSE="stop")
        self.assertEqual(quote.closes_at, 0)
        self.assertEqual(quote.on_close, celaut.ON_CLOSE_REFUSE)

    def test_the_time_is_absolute(self):
        window = dict(ENABLED=True, WINDOWS=[{"START": "08:00", "END": "18:00"}])
        with _settings(**window):
            first = activity_window.next_close(datetime(2026, 9, 4, 9, 0))
            later = activity_window.next_close(datetime(2026, 9, 4, 9, 1))
        self.assertEqual(first, later)

    def test_a_peer_quote_keeps_both_fields_through_the_mu_conversion_and_the_row_terms(self):
        peer_cost = celaut.EstimatedCost(closes_at=1_800_000_000,
                                         on_close=celaut.ON_CLOSE_STOP)
        terms = peer_quotes.maintenance_terms(peer_cost)
        self.assertEqual(terms["on_close"], "stop")
        self.assertEqual(terms["closes_at"], 1_800_000_000)

    def test_a_quote_that_does_not_set_them_reads_as_refuse_and_zero(self):
        terms = peer_quotes.maintenance_terms(celaut.EstimatedCost())
        self.assertEqual((terms["on_close"], terms["closes_at"]), ("refuse", 0))

    def test_the_enum_and_the_names_agree(self):
        for name in ("refuse", "stop", "hibernate"):
            self.assertEqual(on_close.to_name(on_close.to_enum(name)), name)
        self.assertEqual(on_close.to_enum("nonsense"), celaut.ON_CLOSE_REFUSE)
        self.assertEqual(on_close.to_name(99), "refuse")


if __name__ == "__main__":
    unittest.main()
