"""The node's own ledger requests: direct, or through Tor when `LEDGER_VIA_TOR` says so (#552)."""

import json
import unittest
from unittest import mock

try:
    import requests
    from src.routing import ledger_http, policy
    from src.utils.config_validation import ConfigValidationError, validate_routing_config
    IMPORT_ERROR = None
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

EXPLORER = "https://explorer.example/api/v1/boxes"
LOCAL = "http://192.168.1.5:8332/"


def _config(values):
    return mock.patch.object(policy.env_manager, "get", side_effect=lambda k, d=None: values.get(k, d))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WhichWayTests(unittest.TestCase):

    def test_auto_follows_hidden_mode(self):
        with _config({"routing.HIDDEN": True}):
            self.assertTrue(policy.ledger_via_tor())
        with _config({}):
            self.assertFalse(policy.ledger_via_tor())

    def test_the_operator_can_choose_either_way(self):
        with _config({"routing.HIDDEN": True, "routing.tor.LEDGER_VIA_TOR": False}):
            self.assertFalse(policy.ledger_via_tor())
        with _config({"routing.tor.LEDGER_VIA_TOR": True}):
            self.assertTrue(policy.ledger_via_tor())

    def test_an_address_of_the_operators_networks_is_never_sent_through_tor(self):
        with _config({"routing.tor.LEDGER_VIA_TOR": True}):
            self.assertFalse(ledger_http.uses_tor(LOCAL))
            self.assertFalse(ledger_http.uses_tor("http://localhost:9053/info"))
            self.assertTrue(ledger_http.uses_tor(EXPLORER))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RequestTests(unittest.TestCase):

    def test_direct_is_the_plain_requests_call_with_the_same_arguments(self):
        with _config({}), mock.patch.object(requests, "get", return_value="answer") as get, \
                mock.patch.object(ledger_http.tunneled_http, "request") as tor:
            self.assertEqual(ledger_http.get(EXPLORER, params={"limit": 1}, timeout=5), "answer")
        get.assert_called_once_with(EXPLORER, params={"limit": 1}, timeout=5)
        tor.assert_not_called()

    def test_through_tor_no_direct_request_is_made(self):
        with _config({"routing.HIDDEN": True}), \
                mock.patch.object(requests, "get") as get, mock.patch.object(requests, "post") as post, \
                mock.patch.object(ledger_http.tunneled_http, "request",
                                  return_value=(200, b'{"items": []}')) as tor:
            response = ledger_http.get(EXPLORER, params={"limit": 50, "offset": 0}, timeout=7)
        get.assert_not_called()
        post.assert_not_called()
        method, url, timeout = tor.call_args.args[:3]
        self.assertEqual((method, timeout), ("GET", 7))
        self.assertEqual(url, EXPLORER + "?limit=50&offset=0")
        self.assertEqual((response.status_code, response.json()), (200, {"items": []}))

    def test_a_json_post_through_tor_carries_the_body(self):
        with _config({"routing.HIDDEN": True}), mock.patch.object(
                ledger_http.tunneled_http, "request", return_value=(200, b"{}")) as tor:
            ledger_http.post(EXPLORER, json={"registers": {"R4": "x"}})
        method, _, _, body, headers = tor.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(json.loads(body), {"registers": {"R4": "x"}})
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_a_text_post_keeps_its_headers(self):
        with _config({"routing.HIDDEN": True}), mock.patch.object(
                ledger_http.tunneled_http, "request", return_value=(200, b"txid")) as tor:
            response = ledger_http.post(EXPLORER, data="rawtx", headers={"authorization": "Basic x"})
        self.assertEqual(tor.call_args.args[3], "rawtx")
        self.assertEqual(tor.call_args.args[4], {"authorization": "Basic x"})
        self.assertEqual(response.text, "txid")

    def test_tor_not_running_fails_and_never_falls_back_to_direct(self):
        from src.routing.base import RoutingUnavailable
        with _config({"routing.HIDDEN": True}), mock.patch.object(requests, "get") as get, \
                mock.patch.object(ledger_http.tunneled_http, "request",
                                  side_effect=RoutingUnavailable("no tor")):
            with self.assertRaises(requests.exceptions.RequestException):
                ledger_http.get(EXPLORER, timeout=5)
        get.assert_not_called()

    def test_an_error_status_raises_like_requests(self):
        with _config({"routing.HIDDEN": True}), mock.patch.object(
                ledger_http.tunneled_http, "request", return_value=(503, b"")):
            response = ledger_http.get(EXPLORER)
        self.assertFalse(response.ok)
        with self.assertRaises(requests.exceptions.HTTPError):
            response.raise_for_status()

    def test_a_local_node_in_hidden_mode_stays_direct(self):
        with _config({"routing.HIDDEN": True}), mock.patch.object(requests, "post", return_value="ok") as post, \
                mock.patch.object(ledger_http.tunneled_http, "request") as tor:
            ledger_http.post(LOCAL, data="{}", timeout=5)
        post.assert_called_once()
        tor.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ValidationTests(unittest.TestCase):

    def test_the_key_is_true_false_or_auto(self):
        for value in (True, False, "auto"):
            validate_routing_config({"routing": {"tor": {"LEDGER_VIA_TOR": value}}})
        with self.assertRaises(ConfigValidationError):
            validate_routing_config({"routing": {"tor": {"LEDGER_VIA_TOR": "sometimes"}}})


if __name__ == "__main__":
    unittest.main()
