"""Tests for the four-level source registration in ``nodo publish``.

Covers :func:`src.publisher.publisher._announce_source_registration` (the
AUTO_PUBLISH_TX / instance-link / web-page-link / manual-message decision) and
:func:`_submit_source_via_instance_api` (the provisional auto-submit payload).

Follows the repo convention of guarding the import so the suite skips cleanly when
the runtime dependencies (bee_rpc, mnemonic, protos, a loadable config) are absent.
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from src.publisher import publisher
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    publisher = None  # type: ignore[assignment]


def _settings(**overrides):
    """Minimal settings dict shaped like ``_get_publisher_settings`` output."""
    hash_spec = MagicMock()
    hash_spec.id_bytes.hex.return_value = "abcd"
    base = {
        "hash_spec": hash_spec,
        "source_application_web_page": "https://web.example/source?tab=add",
        "auto_publish_tx": False,
        "content_format": ".grpcbb",
        "raw_format": ".celaut",
        "timeout_s": 5,
        "max_retry": 1,
        "backoff_s": 0,
    }
    base.update(overrides)
    return base


def _announce(settings):
    buf = io.StringIO()
    with redirect_stdout(buf):
        publisher._announce_source_registration(
            settings,
            service_id="svcid",
            content_hash="chash",
            manifest_url="https://raw.example/manifest",
        )
    return buf.getvalue()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AnnounceSourceRegistrationTests(unittest.TestCase):
    # ---- Level 1: AUTO_PUBLISH_TX + running instance -> auto submit ----
    def test_level1_auto_submit_success_skips_link(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api", return_value=True) as submit, \
             patch.object(publisher, "_print_click_to_add") as link, \
             patch.object(publisher, "ConfigManager") as cfg:
            cfg.return_value.get.return_value = "word word word"
            out = _announce(_settings(auto_publish_tx=True))

        submit.assert_called_once()
        # The node wallet seed is forwarded to the instance signer.
        self.assertEqual(submit.call_args.kwargs["seed"], "word word word")
        self.assertEqual(submit.call_args.kwargs["file_hash"], "svcid")
        link.assert_not_called()
        self.assertIn("submitted directly", out)

    def test_level1_missing_seed_falls_back_to_instance_link(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api") as submit, \
             patch.object(publisher, "_print_click_to_add") as link, \
             patch.object(publisher, "ConfigManager") as cfg:
            cfg.return_value.get.return_value = ""  # empty WALLET_MNEMONIC
            _announce(_settings(auto_publish_tx=True))

        submit.assert_not_called()
        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], "http://10.0.0.2:9000")

    def test_level1_submit_failure_falls_back_to_instance_link(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api", return_value=False), \
             patch.object(publisher, "_print_click_to_add") as link, \
             patch.object(publisher, "ConfigManager") as cfg:
            cfg.return_value.get.return_value = "seed here"
            _announce(_settings(auto_publish_tx=True))

        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], "http://10.0.0.2:9000")

    # ---- Level 2: running instance, auto disabled -> instance link ----
    def test_level2_instance_link_when_auto_disabled(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api") as submit, \
             patch.object(publisher, "_print_click_to_add") as link:
            _announce(_settings(auto_publish_tx=False))

        submit.assert_not_called()
        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], "http://10.0.0.2:9000")

    # ---- Level 3: no instance, web page configured -> web-page link ----
    def test_level3_web_page_link_when_no_instance(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value=None), \
             patch.object(publisher, "_print_click_to_add") as link:
            _announce(_settings(auto_publish_tx=True))  # auto on but no instance

        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], "https://web.example/source?tab=add")

    # ---- Level 4: no instance, no web page -> manual message ----
    def test_level4_manual_message_when_nothing_configured(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value=None), \
             patch.object(publisher, "_print_click_to_add") as link:
            out = _announce(_settings(source_application_web_page=""))

        link.assert_not_called()
        self.assertIn("Register the source manually", out)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SubmitSourceViaInstanceApiTests(unittest.TestCase):
    def _call(self, request_mock, **overrides):
        kwargs = dict(
            seed="node seed",
            file_hash="svcid",
            content_hash="chash",
            hash_function_id="abcd",
            manifest_url="https://raw.example/manifest",
            content_format=".grpcbb",
            raw_format=".celaut",
            timeout_s=5,
            max_retry=1,
            backoff_s=0,
        )
        kwargs.update(overrides)
        with patch.object(publisher, "_request_with_retry", request_mock):
            return publisher._submit_source_via_instance_api("http://10.0.0.2:9000/", **kwargs)

    def test_posts_to_publish_route_with_seed_and_returns_true(self):
        request_mock = MagicMock(return_value=MagicMock())
        ok = self._call(request_mock)

        self.assertTrue(ok)
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "http://10.0.0.2:9000/publish")  # trailing slash collapsed
        payload = kwargs["json"]
        self.assertEqual(payload["seed"], "node seed")
        self.assertEqual(payload["fileHash"], "svcid")
        # raw != content -> raw fields present
        self.assertEqual(payload["rawFormat"], ".celaut")
        self.assertEqual(payload["rawHash"], "svcid")

    def test_omits_raw_fields_when_formats_match(self):
        request_mock = MagicMock(return_value=MagicMock())
        self._call(request_mock, raw_format=".grpcbb", content_format=".grpcbb")
        payload = request_mock.call_args.kwargs["json"]
        self.assertNotIn("rawFormat", payload)
        self.assertNotIn("rawHash", payload)

    def test_returns_false_on_publisher_error(self):
        request_mock = MagicMock(side_effect=publisher.PublisherError("boom"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = self._call(request_mock)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
