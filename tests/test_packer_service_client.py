#!/usr/bin/env python3
"""Unit tests for the packer-service dependency-upload client.

No Docker/KVM/full pack needed: dependencies are faked on disk and the packer
service is replaced by a tiny in-process HTTP recorder. Injectable dirs +
get_id_fn keep the tests free of nodo's ConfigManager.

Run:  python -m unittest tests.test_packer_service_client   (from the repo root)
"""
import json
import os
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.commands.packer.zip_with_dockerfile.packer_service_client import (
    MissingDependencyError,
    build_dependency_bundle,
    classify_dependency,
    resolve_and_upload_dependencies,
)


def _make_registry(tmp):
    """Create REGISTRY/METADATA/BLOCKS dirs with one packed dependency 'depABC'
    (referencing block 'blkzzz') and return the three dir paths."""
    services = os.path.join(tmp, "registry")
    metadata = os.path.join(tmp, "metadata")
    blocks = os.path.join(tmp, "blocks")
    for d in (services, metadata, blocks):
        os.makedirs(d, exist_ok=True)

    svc = os.path.join(services, "depABC")
    os.makedirs(svc, exist_ok=True)
    with open(os.path.join(svc, "_.json"), "w") as f:
        json.dump([["blkzzz"], "inline0"], f)
    with open(os.path.join(svc, "inline0"), "wb") as f:
        f.write(b"chunkbytes")
    with open(os.path.join(metadata, "depABC"), "wb") as f:
        f.write(b"meta")
    with open(os.path.join(blocks, "blkzzz"), "wb") as f:
        f.write(b"blockbytes")
    return services, metadata, blocks


def _write_pack_config(project_dir, deps):
    os.makedirs(os.path.join(project_dir, ".service"), exist_ok=True)
    with open(os.path.join(project_dir, ".service", "pack_config.json"), "w") as f:
        json.dump({"dependencies": deps}, f)


class _Recorder(BaseHTTPRequestHandler):
    """Fake packer service. present_ids controls GET results; posts are logged."""
    present_ids = set()
    posted = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        sid = self.path.rsplit("/", 1)[-1]
        present = sid in self.present_ids
        body = json.dumps({"service_id": sid, "present": present}).encode()
        self.send_response(200 if present else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        sid = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        type(self).posted.append((sid, data))
        type(self).present_ids.add(sid)
        body = json.dumps({"service_id": sid, "stored": True,
                           "already_present": False, "blocks_added": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.services, _, _ = _make_registry(self.tmp)
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.project, "dependencies", "localdep"))

    def test_registry_direct_hash(self):
        kind, val = classify_dependency(
            "depABC", self.project, self.services, lambda d: "")
        self.assertEqual((kind, val), ("registry", "depABC"))

    def test_registry_via_get_id_tag(self):
        # get_id resolves a tag -> hash present in the registry.
        kind, val = classify_dependency(
            "my-tag", self.project, self.services, lambda d: "depABC")
        self.assertEqual((kind, val), ("registry", "depABC"))

    def test_git(self):
        kind, val = classify_dependency(
            "https://github.com/x/y", self.project, self.services, lambda d: "")
        self.assertEqual(kind, "git")

    def test_local(self):
        kind, val = classify_dependency(
            "dependencies/localdep", self.project, self.services, lambda d: "")
        self.assertEqual(kind, "local")

    def test_missing(self):
        kind, val = classify_dependency(
            "nope-not-here", self.project, self.services, lambda d: "")
        self.assertEqual(kind, "missing")


class BundleTests(unittest.TestCase):
    def test_bundle_layout(self):
        tmp = tempfile.mkdtemp()
        services, metadata, blocks = _make_registry(tmp)
        data = build_dependency_bundle("depABC", services, metadata, blocks)
        import io
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = set(tf.getnames())
        self.assertIn("service/_.json", names)
        self.assertIn("service/inline0", names)
        self.assertIn("metadata", names)
        self.assertIn("blocks/blkzzz", names)

    def test_bundle_missing_service_raises(self):
        tmp = tempfile.mkdtemp()
        services, metadata, blocks = _make_registry(tmp)
        with self.assertRaises(MissingDependencyError):
            build_dependency_bundle("ghost", services, metadata, blocks)


class ResolveUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        _Recorder.present_ids = set()
        _Recorder.posted = []
        self.tmp = tempfile.mkdtemp()
        self.services, self.metadata, self.blocks = _make_registry(self.tmp)
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.project, "dependencies", "localdep"))
        self.url = f"http://127.0.0.1:{self.port}"

    def _resolve(self):
        return resolve_and_upload_dependencies(
            project_directory=self.project,
            packer_service_url=self.url,
            services_dir=self.services,
            metadata_dir=self.metadata,
            blocks_dir=self.blocks,
            get_id_fn=lambda d: "",
        )

    def test_uploads_registry_dep(self):
        _write_pack_config(self.project, {"DEP": "depABC"})
        summary = self._resolve()
        self.assertEqual(summary["uploaded"], ["depABC"])
        self.assertEqual(len(_Recorder.posted), 1)
        # The POSTed body is a valid tar bundle with the expected layout.
        import io
        with tarfile.open(fileobj=io.BytesIO(_Recorder.posted[0][1]), mode="r:gz") as tf:
            self.assertIn("service/_.json", tf.getnames())

    def test_skips_when_already_present(self):
        _Recorder.present_ids = {"depABC"}
        _write_pack_config(self.project, {"DEP": "depABC"})
        summary = self._resolve()
        self.assertEqual(summary["already_present"], ["depABC"])
        self.assertEqual(summary["uploaded"], [])
        self.assertEqual(len(_Recorder.posted), 0)

    def test_missing_dependency_raises(self):
        _write_pack_config(self.project, {"DEP": "ghost-dep"})
        with self.assertRaises(MissingDependencyError):
            self._resolve()

    def test_git_and_local_are_not_uploaded(self):
        _write_pack_config(self.project, {
            "R": "depABC",
            "G": "https://github.com/x/y",
            "L": "dependencies/localdep",
        })
        summary = self._resolve()
        self.assertEqual(summary["uploaded"], ["depABC"])
        self.assertEqual(len(summary["git"]), 1)
        self.assertEqual(len(summary["local"]), 1)

    def test_array_dependencies(self):
        _write_pack_config(self.project, ["depABC"])
        summary = self._resolve()
        self.assertEqual(summary["uploaded"], ["depABC"])

    def test_no_pack_config_is_noop(self):
        summary = self._resolve()
        self.assertEqual(summary["uploaded"], [])


if __name__ == "__main__":
    unittest.main()
