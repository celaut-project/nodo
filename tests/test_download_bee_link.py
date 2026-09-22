"""Tests for ``download_from_manifest_url`` accepting a direct .celaut.bee link.

Covers the new branch in :func:`src.commands.publisher.publisher.download_from_manifest_url`
that downloads a `.celaut.bee` artifact directly (single HTTPS GET) instead of treating
the URL as a plain-text manifest of chunk URLs, alongside the pre-existing manifest path.

Follows the repo convention of guarding the import so the suite skips cleanly when the
runtime dependencies (bee_rpc, protos, a loadable config) are absent.
"""

import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from src.commands.publisher import publisher
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    publisher = None  # type: ignore[assignment]


def _settings(tmp_path, **overrides):
    hash_spec = MagicMock()
    base = {
        "token": "",
        "output_dir": str(tmp_path),
        "timeout_s": 5,
        "max_retry": 1,
        "backoff_s": 0,
        "hash_spec": hash_spec,
        "keep_artifacts": True,
        "auto_import": False,
    }
    base.update(overrides)
    return base


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DownloadDirectBeeLinkTests(unittest.TestCase):
    def test_direct_celaut_bee_url_is_downloaded_in_one_request(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            url = "https://example.com/path/to/service.celaut.bee"
            fetch_mock = MagicMock(return_value=b"raw-bee-bytes")

            with patch.object(publisher, "_get_publisher_settings", return_value=_settings(tmp_path)), \
                 patch.object(publisher, "_fetch_bytes", fetch_mock):
                result = publisher.download_from_manifest_url(url)

            fetch_mock.assert_called_once()  # single request, no per-chunk fetches
            output_path = Path(result["output_path"])
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_bytes(), b"raw-bee-bytes")
            self.assertEqual(result["manifest"], [url])

    def test_non_utf8_response_falls_back_to_direct_bee_even_without_extension(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            url = "https://example.com/download/123"  # no .celaut.bee suffix
            fetch_mock = MagicMock(return_value=b"\xff\xfe\x00binary")

            with patch.object(publisher, "_get_publisher_settings", return_value=_settings(tmp_path)), \
                 patch.object(publisher, "_fetch_bytes", fetch_mock):
                result = publisher.download_from_manifest_url(url)

            fetch_mock.assert_called_once()
            output_path = Path(result["output_path"])
            self.assertEqual(output_path.read_bytes(), b"\xff\xfe\x00binary")

    def test_manifest_url_still_downloads_chunks(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            manifest_url = "https://raw.example/uploads/svc/manifest"
            chunk_urls = [
                "https://raw.example/uploads/svc/chunk_0000",
                "https://raw.example/uploads/svc/chunk_0001",
            ]
            manifest_text = "\n".join(chunk_urls) + "\n"

            def fetch_side_effect(url, **kwargs):
                if url == manifest_url:
                    return manifest_text.encode("utf-8")
                return b"chunk-data-" + url.encode("utf-8")[-1:]

            fetch_mock = MagicMock(side_effect=fetch_side_effect)

            with patch.object(publisher, "_get_publisher_settings", return_value=_settings(tmp_path)), \
                 patch.object(publisher, "_fetch_bytes", fetch_mock):
                result = publisher.download_from_manifest_url(manifest_url)

            self.assertEqual(fetch_mock.call_count, 1 + len(chunk_urls))
            self.assertEqual(result["manifest"], chunk_urls)


if __name__ == "__main__":
    unittest.main()
