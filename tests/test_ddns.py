"""Tests for the DDNS updater (``src/manager/ddns.py``).

No test touches the network: ``requests.get`` is replaced, so what is asserted is
which request would go out, how each provider answer is interpreted, and that the
manager-loop tick stays a cheap, silent no-op when it should.
"""

import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    import requests

    from src.manager import ddns
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ddns = None  # type: ignore[assignment]


def _response(status_code: int = 200, text: str = "good") -> MagicMock:
    answer = MagicMock()
    answer.status_code = status_code
    answer.text = text
    return answer


def _settings(**overrides):
    """Patch only the DDNS keys; ConfigManager is a shared singleton, so anything
    else must still reach the real config."""
    settings = {
        "ddns.ENABLED": True,
        "ddns.PROVIDER": "desec",
        "ddns.DOMAIN": "my-node.dedyn.io",
        "ddns.TOKEN": "secret-token",
        "ddns.INTERVAL_SECONDS": 600,
        "network.PUBLIC_IP": "",
    }
    settings.update(overrides)
    real_get = ddns.env_manager.get

    def get(key, default=None):
        return settings[key] if key in settings else real_get(key, default)

    return patch.object(ddns.env_manager, "get", side_effect=get)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DdnsConfigTests(unittest.TestCase):
    def test_a_bad_interval_falls_back_to_the_default(self):
        with _settings(**{"ddns.INTERVAL_SECONDS": "soon"}):
            self.assertEqual(ddns.interval_seconds(), ddns.DEFAULT_INTERVAL_SECONDS)

        with _settings(**{"ddns.INTERVAL_SECONDS": 0}):
            self.assertEqual(ddns.interval_seconds(), ddns.DEFAULT_INTERVAL_SECONDS)

    def test_an_unknown_provider_falls_back_to_desec(self):
        with _settings(**{"ddns.PROVIDER": "no-ip"}):
            name, _ = ddns._provider()
        self.assertEqual(name, ddns.DESEC)

    def test_no_pinned_ip_means_the_provider_uses_the_source_address(self):
        with _settings():
            self.assertIsNone(ddns.configured_public_ip())

    def test_a_pinned_ip_is_used_when_set(self):
        with _settings(**{"network.PUBLIC_IP": "203.0.113.7"}):
            self.assertEqual(ddns.configured_public_ip(), "203.0.113.7")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DdnsPublishTests(unittest.TestCase):
    def _publish(self, response, **overrides):
        get = MagicMock(return_value=response)
        with _settings(**overrides), patch.object(
            ddns.requests, "get", get
        ), patch.object(ddns, "resolved_ip", return_value="203.0.113.7"):
            try:
                return ddns.publish_public_ip(), get, None
            except ddns.DdnsError as e:
                return False, get, str(e)

    def test_a_good_answer_is_a_successful_update(self):
        ok, get, error = self._publish(_response(text="good"))

        self.assertTrue(ok)
        self.assertIsNone(error)
        url, kwargs = get.call_args[0][0], get.call_args[1]
        self.assertEqual(url, ddns.DESEC_UPDATE_URL)
        self.assertEqual(kwargs["params"]["hostname"], "my-node.dedyn.io")
        self.assertEqual(kwargs["headers"]["Authorization"], "Token secret-token")
        # No address sent: deSEC records the source address.
        self.assertNotIn("myipv4", kwargs["params"])

    def test_nochg_is_also_success(self):
        """The record already held this value; that is not a failure."""
        ok, _, error = self._publish(_response(text="nochg 203.0.113.7"))

        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_a_pinned_ip_is_sent_as_myipv4(self):
        _, get, _ = self._publish(
            _response(), **{"network.PUBLIC_IP": "203.0.113.7"}
        )

        self.assertEqual(get.call_args[1]["params"]["myipv4"], "203.0.113.7")

    def test_a_rejected_token_is_reported_clearly(self):
        ok, _, error = self._publish(_response(status_code=401, text=""))

        self.assertFalse(ok)
        self.assertIn("token", error)

    def test_an_unknown_hostname_is_reported_clearly(self):
        ok, _, error = self._publish(_response(status_code=404, text=""))

        self.assertFalse(ok)
        self.assertIn("hostname", error)

    def test_an_unexpected_body_is_not_taken_as_success(self):
        ok, _, error = self._publish(_response(text="abuse"))

        self.assertFalse(ok)
        self.assertIn("abuse", error)

    def test_a_network_failure_is_reported_not_raised_raw(self):
        get = MagicMock(side_effect=requests.RequestException("no route"))
        with _settings(), patch.object(ddns.requests, "get", get):
            with self.assertRaises(ddns.DdnsError) as caught:
                ddns.publish_public_ip()

        self.assertIn("cannot reach", str(caught.exception))

    def test_missing_domain_or_token_is_refused_before_any_request(self):
        get = MagicMock()
        for missing in ("ddns.DOMAIN", "ddns.TOKEN"):
            with _settings(**{missing: ""}), patch.object(ddns.requests, "get", get):
                with self.assertRaises(ddns.DdnsError):
                    ddns.publish_public_ip()

        get.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DdnsTickTests(unittest.TestCase):
    def setUp(self):
        ddns._last_tick_monotonic = None

    def tearDown(self):
        ddns._last_tick_monotonic = None

    def test_disabled_does_nothing_at_all(self):
        publish = MagicMock()
        with _settings(**{"ddns.ENABLED": False}), patch.object(
            ddns, "publish_public_ip", publish
        ):
            ddns.ddns_tick()

        publish.assert_not_called()

    def test_it_publishes_on_the_first_tick_then_gates_until_the_interval(self):
        publish = MagicMock(return_value=True)
        with _settings(), patch.object(ddns, "publish_public_ip", publish):
            ddns.ddns_tick()
            ddns.ddns_tick()  # Immediately again: must be a no-op.
            ddns.ddns_tick()

        self.assertEqual(publish.call_count, 1)

    def test_the_gate_opens_once_the_interval_has_passed(self):
        publish = MagicMock(return_value=True)
        with _settings(**{"ddns.INTERVAL_SECONDS": 1}), patch.object(
            ddns, "publish_public_ip", publish
        ):
            ddns.ddns_tick()
            # Pretend the last tick was long ago rather than sleeping. Step past
            # the effective interval (which is floored to MIN_INTERVAL_SECONDS).
            ddns._last_tick_monotonic -= ddns.interval_seconds() + 1
            ddns.ddns_tick()

        self.assertEqual(publish.call_count, 2)

    def test_a_failed_update_never_escapes_into_the_manager_loop(self):
        """The manager loop must survive a provider being down."""
        for failure in (ddns.DdnsError("provider down"), RuntimeError("boom")):
            ddns._last_tick_monotonic = None
            with _settings(), patch.object(
                ddns, "publish_public_ip", side_effect=failure
            ):
                ddns.ddns_tick()  # Must not raise.

    def test_status_reports_what_is_configured_and_what_resolves(self):
        with _settings(), patch.object(ddns, "resolved_ip", return_value="203.0.113.7"):
            reported = ddns.status()

        self.assertTrue(reported["enabled"])
        self.assertEqual(reported["hostname"], "my-node.dedyn.io")
        self.assertEqual(reported["resolves_to"], "203.0.113.7")
        self.assertIsNone(reported["configured_ip"])
        # The token must never be part of a status view.
        self.assertNotIn("secret-token", str(reported))


if __name__ == "__main__":
    unittest.main()
