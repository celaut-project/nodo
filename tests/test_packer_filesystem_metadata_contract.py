import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from src.utils.filesystem_xattrs import (
    FILESYSTEM_METADATA_KEYS,
    parse_filesystem_metadata_xattrs,
)

IMPORT_ERROR = None
try:
    from src.packers import zip_with_dockerfile as packer_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    packer_module = None  # type: ignore[assignment]


def _all_branches(fs):
    branches = []
    for branch in fs.branch:
        branches.append(branch)
        if branch.HasField("filesystem"):
            branches.extend(_all_branches(branch.filesystem))
    return branches


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackerFilesystemMetadataContractTests(unittest.TestCase):
    def test_parse_container_emits_metadata_contract_for_file_dir_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = os.path.join(tmpdir, "cache")
            aux_id = "svc"
            fs_root = os.path.join(cache_root, aux_id, "filesystem")
            os.makedirs(fs_root, exist_ok=True)

            file_path = os.path.join(fs_root, "run.sh")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\necho ok\n")

            dir_path = os.path.join(fs_root, "config")
            os.makedirs(dir_path, exist_ok=True)

            link_path = os.path.join(fs_root, "run-link")
            os.symlink("run.sh", link_path)

            packer = packer_module.ZipContainerPacker.__new__(packer_module.ZipContainerPacker)
            packer.blocks = []
            packer.service = packer_module.pack_pb2.Service()
            packer.metadata = packer_module.celaut.Metadata()
            packer.aux_id = aux_id
            packer.json = {"resources": {}, "init": {}, "architecture": "linux/amd64"}

            with patch.object(packer_module, "CACHE", cache_root + "/"), patch.object(
                packer_module, "MIN_BUFFER_BLOCK_SIZE", 10**9
            ):
                packer.parseContainer()

            branches = _all_branches(packer.service.container.filesystem)
            self.assertGreaterEqual(len(branches), 3)

            expected_keys = set(FILESYSTEM_METADATA_KEYS)
            for branch in branches:
                self.assertTrue(expected_keys.issubset(set(branch.xattrs.keys())))
                parsed = parse_filesystem_metadata_xattrs(branch.xattrs)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.device_major, 0)
                self.assertEqual(parsed.device_minor, 0)
                self.assertFalse(parsed.device_is_block)

            parsed_by_name = {
                branch.name: parse_filesystem_metadata_xattrs(branch.xattrs)
                for branch in branches
            }
            self.assertTrue(stat.S_ISREG(parsed_by_name["run.sh"].mode))
            self.assertTrue(stat.S_ISDIR(parsed_by_name["config"].mode))
            self.assertTrue(stat.S_ISLNK(parsed_by_name["run-link"].mode))


if __name__ == "__main__":
    unittest.main()
