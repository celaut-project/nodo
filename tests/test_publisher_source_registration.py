"""Tests for the four-level source registration in ``nodo publish``.

Covers :func:`src.publisher.publisher._announce_source_registration` (the
AUTO_PUBLISH_TX / web-page-link / manual-message decision) and
:func:`_submit_source_via_instance_api` (the confirmed ``POST /api/sources`` payload).

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
    # ---- Level 1: AUTO_PUBLISH_TX + running instance + profile box -> auto submit ----
    def test_level1_auto_submit_success_skips_link(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api", return_value=True) as submit, \
             patch.object(publisher, "_print_click_to_add") as link, \
             patch.object(publisher, "ConfigManager") as cfg:
            cfg.return_value.get.return_value = "profilebox64hex"
            out = _announce(_settings(auto_publish_tx=True))

        submit.assert_called_once()
        # The node's PROFILE box id is forwarded as the opinion author; no seed over HTTP.
        self.assertEqual(submit.call_args.kwargs["main_box_id"], "profilebox64hex")
        self.assertEqual(submit.call_args.kwargs["file_hash"], "svcid")
        self.assertNotIn("seed", submit.call_args.kwargs)
        link.assert_not_called()
        self.assertIn("submitted directly", out)

    def test_level1_missing_profile_box_falls_back_to_web_link(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api") as submit, \
             patch.object(publisher, "_print_click_to_add") as link, \
             patch.object(publisher, "ConfigManager") as cfg:
            cfg.return_value.get.return_value = ""  # empty SOURCE_PROFILE_BOX_ID
            _announce(_settings(auto_publish_tx=True))

        submit.assert_not_called()
        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], "https://web.example/source?tab=add")

    def test_level1_submit_failure_falls_back_to_web_link(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api", return_value=False), \
             patch.object(publisher, "_print_click_to_add") as link, \
             patch.object(publisher, "ConfigManager") as cfg:
            cfg.return_value.get.return_value = "profilebox64hex"
            _announce(_settings(auto_publish_tx=True))

        link.assert_called_once()
        self.assertEqual(link.call_args.args[0], "https://web.example/source?tab=add")

    # ---- Level 2: running instance, auto disabled -> web-page link (NOT the instance) ----
    def test_level2_web_link_when_auto_disabled(self):
        with patch.object(publisher, "_resolve_source_application_endpoint", return_value="http://10.0.0.2:9000"), \
             patch.object(publisher, "_submit_source_via_instance_api") as submit, \
             patch.object(publisher, "_print_click_to_add") as link:
            _announce(_settings(auto_publish_tx=False))

        submit.assert_not_called()
        link.assert_called_once()
        # The instance serves only /api + /mcp; the prefill link must target the web app.
        self.assertEqual(link.call_args.args[0], "https://web.example/source?tab=add")

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
    @staticmethod
    def _response(body):
        """A stand-in for _request_with_retry's return: an object with .content bytes."""
        import json as _json
        resp = MagicMock()
        resp.content = _json.dumps(body).encode("utf-8")
        return resp

    def _call(self, request_mock, **overrides):
        kwargs = dict(
            main_box_id="profilebox64hex",
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

    def test_posts_to_api_sources_with_source_entry_and_returns_true(self):
        request_mock = MagicMock(return_value=self._response({"submitted": True, "txId": "tx1"}))
        ok = self._call(request_mock)

        self.assertTrue(ok)
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "http://10.0.0.2:9000/api/sources")  # trailing slash collapsed
        payload = kwargs["json"]
        self.assertEqual(payload["mainBoxId"], "profilebox64hex")
        self.assertEqual(payload["fileHash"], "svcid")
        self.assertNotIn("seed", payload)  # the mnemonic is never sent over HTTP
        entry = payload["sourceEntry"]
        self.assertEqual(entry["hashFunctionId"], "abcd")
        self.assertEqual(entry["contentHash"], "chash")
        self.assertEqual(entry["rawFormat"], ".celaut")
        self.assertEqual(entry["urlLink"], "https://raw.example/manifest")
        self.assertIs(entry["isChunked"], True)

    def test_unsigned_response_returns_false(self):
        # An unsigned-mode instance returns an unsigned tx: not a real submit.
        request_mock = MagicMock(
            return_value=self._response({"submitted": False, "unsignedTransaction": {"inputs": []}})
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = self._call(request_mock)
        self.assertFalse(ok)
        self.assertIn("unsigned mode", buf.getvalue())

    def test_returns_false_on_publisher_error(self):
        request_mock = MagicMock(side_effect=publisher.PublisherError("boom"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = self._call(request_mock)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
