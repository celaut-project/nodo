"""`source_download.download_to_file` -- the capped, guarded download of a service source.

The paths that a service or a peer can trigger (`maintain.check_wanted_service`) download
from a URL that a public registry gave. The tests serve from 127.0.0.1, which the guard
refuses, so they patch `host_is_allowed` and test the guard on its own.
"""
import http.server
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.core_services import source_download as sd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    sd = None  # type: ignore[assignment]

MIB = 1 << 20


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves what `server.routes` says: path -> (status, headers, body or a generator)."""

    def log_message(self, *args):  # keep the test output quiet
        pass

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        status, headers, body = self.server.routes.get(self.path, (404, {}, b"not found"))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if isinstance(body, (bytes, bytearray)):
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:  # no Content-Length: the connection closing ends the body
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in body:
                self.wfile.write(chunk)
        self.close_connection = True


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DownloadToFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo-source-download-")
        self.addCleanup(self._tmp.cleanup)
        self.dest = os.path.join(self._tmp.name, "out.bee")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.routes = {}
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        patcher = patch.object(sd, "host_is_allowed", lambda host: host == "127.0.0.1")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read(self):
        with open(self.dest, "rb") as f:
            return f.read()

    def test_a_celaut_bee_url_is_the_file(self):
        self.server.routes["/s.celaut.bee"] = (200, {}, b"\x00\x01binary")

        written = sd.download_to_file(f"{self.base}/s.celaut.bee", self.dest, max_bytes=0)

        self.assertEqual(self._read(), b"\x00\x01binary")
        self.assertEqual(written, 8)

    def test_a_url_with_other_binary_content_is_the_file_too(self):
        self.server.routes["/download"] = (200, {}, b"\xff\xfe\x00 not text")

        sd.download_to_file(f"{self.base}/download", self.dest, max_bytes=0)

        self.assertEqual(self._read(), b"\xff\xfe\x00 not text")

    def test_a_manifest_of_two_parts_is_joined_in_order(self):
        self.server.routes["/manifest"] = (
            200, {}, f"{self.base}/p1\n\n{self.base}/p2\n".encode()
        )
        self.server.routes["/p1"] = (200, {}, b"AAAA")
        self.server.routes["/p2"] = (200, {}, b"BB")

        written = sd.download_to_file(f"{self.base}/manifest", self.dest, max_bytes=0)

        self.assertEqual(self._read(), b"AAAABB")
        self.assertEqual(written, 6)

    def test_the_limit_is_for_all_the_parts_together(self):
        self.server.routes["/manifest"] = (200, {}, f"{self.base}/p1\n{self.base}/p2".encode())
        self.server.routes["/p1"] = (200, {}, b"A" * 600)
        self.server.routes["/p2"] = (200, {}, b"B" * 600)

        with self.assertRaises(sd.SourceDownloadError):
            sd.download_to_file(f"{self.base}/manifest", self.dest, max_bytes=1000)

    def test_a_declared_length_over_the_limit_is_refused_before_reading(self):
        self.server.routes["/big.celaut.bee"] = (200, {}, b"A" * 2000)

        with self.assertRaises(sd.SourceDownloadError) as caught:
            sd.download_to_file(f"{self.base}/big.celaut.bee", self.dest, max_bytes=1000)

        self.assertIn("larger than the limit", str(caught.exception))
        self.assertEqual(self._read(), b"")  # nothing of it was written

    def test_a_body_that_does_not_say_its_length_is_cut_at_the_limit(self):
        def endless():
            for _ in range(100):
                yield b"A" * 100

        self.server.routes["/s.celaut.bee"] = (200, {}, endless())

        with self.assertRaises(sd.SourceDownloadError):
            sd.download_to_file(f"{self.base}/s.celaut.bee", self.dest, max_bytes=1000)

        self.assertLessEqual(len(self._read()), 1000 + sd.CHUNK_BYTES)

    def test_zero_is_no_limit(self):
        self.server.routes["/s.celaut.bee"] = (200, {}, b"A" * (3 * MIB))

        self.assertEqual(
            sd.download_to_file(f"{self.base}/s.celaut.bee", self.dest, max_bytes=0), 3 * MIB
        )

    def test_a_redirect_to_a_host_that_is_not_allowed_is_refused(self):
        self.server.routes["/s.celaut.bee"] = (
            302, {"Location": f"http://localhost:{self.server.server_address[1]}/x"}, b""
        )

        with self.assertRaises(sd.SourceDownloadError) as caught:
            sd.download_to_file(f"{self.base}/s.celaut.bee", self.dest, max_bytes=0)

        self.assertIn("not a public host", str(caught.exception))

    def test_a_redirect_to_an_allowed_host_is_followed(self):
        self.server.routes["/s.celaut.bee"] = (302, {"Location": "/real.celaut.bee"}, b"")
        self.server.routes["/real.celaut.bee"] = (200, {}, b"payload")

        sd.download_to_file(f"{self.base}/s.celaut.bee", self.dest, max_bytes=0)

        self.assertEqual(self._read(), b"payload")

    def test_a_loop_of_redirects_ends(self):
        self.server.routes["/a.celaut.bee"] = (302, {"Location": "/a.celaut.bee"}, b"")

        with self.assertRaises(sd.SourceDownloadError) as caught:
            sd.download_to_file(f"{self.base}/a.celaut.bee", self.dest, max_bytes=0)

        self.assertIn("redirects", str(caught.exception))
        self.assertEqual(len(self.server.requests), sd.MAX_REDIRECTS + 1)

    def test_only_http_and_https(self):
        for url in ("file:///etc/passwd", "ftp://127.0.0.1/s.celaut.bee", "gopher://x/y"):
            with self.assertRaises(sd.SourceDownloadError):
                sd.download_to_file(url, self.dest, max_bytes=0)

    def test_an_error_status_is_an_error(self):
        with self.assertRaises(sd.SourceDownloadError) as caught:
            sd.download_to_file(f"{self.base}/missing.celaut.bee", self.dest, max_bytes=0)

        self.assertIn("404", str(caught.exception))

    def test_no_credentials_are_sent(self):
        self.server.routes["/s.celaut.bee"] = (200, {}, b"x")

        sd.download_to_file(f"{self.base}/s.celaut.bee", self.dest, max_bytes=0)

        sent = {k.lower() for k in self.server.requests[0][1]}
        self.assertFalse(sent & {"authorization", "cookie"})

    def test_an_unreachable_source_is_an_error_and_not_an_exception_of_requests(self):
        with self.assertRaises(sd.SourceDownloadError):
            sd.download_to_file("http://127.0.0.1:1/s.celaut.bee", self.dest, max_bytes=0, timeout_s=2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PublicHostTests(unittest.TestCase):
    def test_addresses_that_are_not_public_are_refused(self):
        for host in (
            "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.5.4", "192.168.1.1",
            "169.254.169.254", "0.0.0.0", "100.64.0.1", "::1", "fe80::1", "fc00::1",
            "::ffff:127.0.0.1", "::ffff:10.0.0.1",
        ):
            self.assertFalse(sd.is_public_host(host), host)

    def test_a_public_address_is_allowed(self):
        self.assertTrue(sd.is_public_host("8.8.8.8"))
        self.assertTrue(sd.is_public_host("2606:4700:4700::1111"))

    def test_a_name_that_does_not_resolve_is_refused(self):
        self.assertFalse(sd.is_public_host("name.that.does.not.exist.invalid"))

    def test_one_address_that_is_not_public_is_enough_to_refuse_a_name(self):
        answers = [(2, 1, 6, "", ("8.8.8.8", 0)), (2, 1, 6, "", ("10.0.0.5", 0))]
        with patch.object(sd.socket, "getaddrinfo", return_value=answers):
            self.assertFalse(sd.is_public_host("both.example"))


if __name__ == "__main__":
    unittest.main()
