"""Tests for ``nodo publish`` uploading to a GitHub Release (issue #495).

``_upload_to_release`` runs against ``FakeReleaseProvider``, an in-memory stand-in for
the release API that keeps the same methods as ``GitHubReleaseProvider``. The published
assets are then fed to ``download_from_manifest_url`` through a fake ``_fetch_bytes``,
so each case also checks that the download side joins the parts back.

Follows the repo convention of guarding the import so the suite skips cleanly when
the runtime dependencies (bee_rpc, protos, a loadable config) are absent.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from src.commands.publisher import publisher
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    publisher = None  # type: ignore[assignment]

SERVICE_ID = "ab" * 32
REPO = "owner/repo"


class FakeReleaseProvider:
    """In-memory releases. ``fail_on`` names an asset whose upload raises."""

    def __init__(self, fail_on=None):
        self.repo = REPO
        self.releases = {}
        self.next_id = 1
        self.fail_on = fail_on
        self.uploads = []

    def check_access(self):
        pass

    def find_release(self, tag):
        for release in self.releases.values():
            if release["tag_name"] == tag:
                return self._public(release)
        return None

    def _public(self, release):
        return {k: v for k, v in release.items() if k != "files"}

    def create_draft_release(self, tag, title, body):
        release = {
            "id": self.next_id,
            "tag_name": tag,
            "draft": True,
            "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
            "files": {},
        }
        self.releases[self.next_id] = release
        self.next_id += 1
        return self._public(release)

    def update_release(self, release_id, **fields):
        self.releases[release_id].update(fields)
        return self._public(self.releases[release_id])

    def delete_release(self, release_id):
        del self.releases[release_id]

    def list_assets(self, release_id):
        files = self.releases[release_id]["files"]
        return [
            {"id": (release_id, name), "name": name, "size": len(data), "state": "uploaded"}
            for name, data in files.items()
        ]

    def delete_asset(self, asset_id):
        release_id, name = asset_id
        del self.releases[release_id]["files"][name]

    def upload_asset(self, release_id, name, body_factory, size):
        if name == self.fail_on:
            raise publisher.PublisherError("HTTP 502 uploading", status_code=502)
        body = body_factory()
        data = body if isinstance(body, bytes) else b"".join(body)
        assert len(data) == size, (name, len(data), size)
        self.releases[release_id]["files"][name] = data
        self.uploads.append(name)
        return {"name": name, "size": size}

    def download_url(self, tag, name):
        return f"https://github.com/{REPO}/releases/download/{tag}/{name}"

    def fetch(self, url, **kwargs):
        """Serve a published asset by its download URL, as GitHub would."""
        prefix = f"https://github.com/{REPO}/releases/download/"
        tag, name = url[len(prefix):].split("/", 1)
        for release in self.releases.values():
            if release["tag_name"] == tag and not release["draft"]:
                return release["files"][name]
        raise publisher.PublisherError(f"HTTP 404 calling GET {url}", status_code=404)


def _upload(provider, path, split_size, part_size):
    with redirect_stdout(io.StringIO()):
        return publisher._upload_to_release(
            source_path=path,
            provider=provider,
            split_size=split_size,
            part_size=part_size,
            service_id=SERVICE_ID,
        )


def _download(provider, url, tmp_path):
    settings = {
        "token": "",
        "output_dir": str(tmp_path),
        "timeout_s": 5,
        "max_retry": 1,
        "backoff_s": 0,
        "keep_artifacts": True,
        "auto_import": False,
    }
    with patch.object(publisher, "_get_publisher_settings", return_value=settings), \
         patch.object(publisher, "_fetch_bytes", side_effect=provider.fetch), \
         redirect_stdout(io.StringIO()):
        result = publisher.download_from_manifest_url(url)
    return Path(result["output_path"]).read_bytes()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UploadToReleaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.data = bytes(range(256)) * 40  # 10240 bytes
        self.bee = self.tmp_path / "service.celaut.bee"
        self.bee.write_bytes(self.data)
        self.downloads = self.tmp_path / "downloads"

    def tearDown(self):
        self._tmp.cleanup()

    def test_small_file_is_one_asset_with_a_direct_url(self):
        provider = FakeReleaseProvider()
        result = _upload(provider, self.bee, split_size=len(self.data), part_size=1000)

        self.assertFalse(result["is_chunked"])
        self.assertEqual(
            result["manifest_url"],
            f"https://github.com/{REPO}/releases/download/celaut-{SERVICE_ID}/{SERVICE_ID}.celaut.bee",
        )
        (release,) = provider.releases.values()
        self.assertFalse(release["draft"])
        self.assertEqual(list(release["files"]), [f"{SERVICE_ID}.celaut.bee"])
        self.assertEqual(_download(provider, result["manifest_url"], self.downloads), self.data)

    def test_file_above_split_size_is_parts_and_a_manifest(self):
        provider = FakeReleaseProvider()
        result = _upload(provider, self.bee, split_size=4096, part_size=3000)

        self.assertTrue(result["is_chunked"])
        self.assertEqual(result["total_parts"], 4)  # 3000 + 3000 + 3000 + 1240
        self.assertTrue(result["manifest_url"].endswith(f"/celaut-{SERVICE_ID}/manifest"))
        (release,) = provider.releases.values()
        self.assertFalse(release["draft"])
        self.assertEqual(provider.uploads[-1], "manifest")  # uploaded after every part
        manifest_lines = release["files"]["manifest"].decode("utf-8").split()
        self.assertEqual(len(manifest_lines), 4)
        for line in manifest_lines:
            self.assertTrue(line.startswith(f"https://github.com/{REPO}/releases/download/"))
        self.assertEqual(_download(provider, result["manifest_url"], self.downloads), self.data)

    def test_second_publish_reuses_the_release(self):
        provider = FakeReleaseProvider()
        first = _upload(provider, self.bee, split_size=4096, part_size=3000)
        uploads_after_first = list(provider.uploads)

        second = _upload(provider, self.bee, split_size=4096, part_size=3000)

        self.assertEqual(len(provider.releases), 1)  # no duplicate release
        self.assertEqual(provider.uploads, uploads_after_first)  # nothing uploaded again
        self.assertTrue(second["reused"])
        self.assertEqual(second["manifest_url"], first["manifest_url"])

    def test_second_publish_with_other_sizes_replaces_the_assets(self):
        provider = FakeReleaseProvider()
        _upload(provider, self.bee, split_size=4096, part_size=3000)

        result = _upload(provider, self.bee, split_size=len(self.data), part_size=3000)

        (release,) = provider.releases.values()
        self.assertFalse(release["draft"])
        self.assertEqual(list(release["files"]), [f"{SERVICE_ID}.celaut.bee"])
        self.assertEqual(_download(provider, result["manifest_url"], self.downloads), self.data)

    def test_failed_upload_deletes_the_release(self):
        provider = FakeReleaseProvider(fail_on=f"{SERVICE_ID}.celaut.bee.part0002")

        with self.assertRaises(publisher.PublisherError):
            _upload(provider, self.bee, split_size=4096, part_size=3000)

        self.assertEqual(provider.releases, {})  # no release points to missing parts

    def test_leftover_draft_is_reused_and_published(self):
        provider = FakeReleaseProvider()
        draft = provider.create_draft_release(f"celaut-{SERVICE_ID}", "t", "b")
        provider.releases[draft["id"]]["files"]["stale"] = b"x"

        _upload(provider, self.bee, split_size=4096, part_size=3000)

        (release,) = provider.releases.values()
        self.assertEqual(release["id"], draft["id"])
        self.assertFalse(release["draft"])
        self.assertNotIn("stale", release["files"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GitHubReleaseProviderTests(unittest.TestCase):
    def _provider(self, max_retry=1):
        return publisher.GitHubReleaseProvider(
            token="t", repo=REPO, target="", timeout_s=5, max_retry=max_retry, backoff_s=0
        )

    @staticmethod
    def _response(body):
        response = MagicMock()
        response.json.return_value = body
        return response

    def test_token_without_push_permission_is_refused(self):
        request = MagicMock(return_value=self._response({"permissions": {"push": False}}))
        with patch.object(publisher, "_request_with_retry", request):
            with self.assertRaises(publisher.PublisherError) as ctx:
                self._provider().check_access()
        self.assertIn("cannot create releases", str(ctx.exception))
        self.assertIn("Contents: Read and write", str(ctx.exception))

    def test_release_creation_forbidden_says_which_permission(self):
        request = MagicMock(side_effect=publisher.PublisherError("HTTP 403", status_code=403))
        with patch.object(publisher, "_request_with_retry", request):
            with self.assertRaises(publisher.PublisherError) as ctx:
                self._provider().create_draft_release("celaut-x", "t", "b")
        self.assertIn("cannot create releases", str(ctx.exception))

    def test_empty_repository_is_explained(self):
        def request(method, url, **kwargs):
            if url.endswith("/commits"):
                raise publisher.PublisherError("HTTP 409", status_code=409)
            return self._response({"permissions": {"push": True}})

        with patch.object(publisher, "_request_with_retry", side_effect=request):
            with self.assertRaises(publisher.PublisherError) as ctx:
                self._provider().check_access()
        self.assertIn("is empty", str(ctx.exception))

    def test_upload_retry_removes_the_half_uploaded_asset(self):
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url))
            if method == "POST" and len([c for c in calls if c[0] == "POST"]) == 1:
                raise publisher.PublisherError("HTTP 502", status_code=502)
            if method == "GET":
                return self._response([{"id": 7, "name": "a.bin", "state": "starter"}])
            return self._response({"name": "a.bin"})

        with patch.object(publisher, "_request_with_retry", side_effect=request), \
             patch.object(publisher.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            self._provider(max_retry=3).upload_asset(1, "a.bin", lambda: b"abc", 3)

        self.assertIn(("DELETE", f"https://api.github.com/repos/{REPO}/releases/assets/7"), calls)
        self.assertEqual([c[0] for c in calls if c[0] == "POST"], ["POST", "POST"])

    def test_file_part_streams_only_its_range(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "f"
            path.write_bytes(b"0123456789")
            part = publisher._FilePart(path, 3, 4)
            self.assertEqual(len(part), 4)
            self.assertEqual(b"".join(part), b"3456")
            part.close()

    def test_sizes_above_the_asset_limit_are_refused(self):
        config = MagicMock()
        values = {
            "publisher.REPOSITORY": REPO,
            "publisher.PART_SIZE_MB": 4096,
        }
        config.get.side_effect = lambda key, default=None: values.get(key, default)
        with patch.object(publisher, "_resolve_token", return_value="t"), \
             patch.object(publisher, "get_configured_hash_spec", return_value=MagicMock()):
            with self.assertRaises(publisher.PublisherError) as ctx:
                publisher._get_publisher_settings(config)
        self.assertIn("PART_SIZE_MB", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
