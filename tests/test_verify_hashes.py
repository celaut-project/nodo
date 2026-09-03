"""verify.py's hash calculators, and the invariants callers rely on.

`calculate_hashes` / `calculate_hashes_by_stream` are the source of every
per-object HashTag this node ever writes: the packed filesystem's own
HashTag (`zip_with_dockerfile.py`'s `parseFilesys`) and, through
`get_service_list_of_hashes`, a service's plain content hash. Both now record
BLAKE2B alongside SHA3_256 and SHAKE_256, so a service stays verifiable under
BLAKE2B without a repack if a node's `hashing.HASH` is ever set to it.
"""
import hashlib
import unittest

from src.utils.hashing import BLAKE2B_ID, SHA3_256_ID, SHAKE_256_ID
from src.utils.verify import (
    calculate_hashes,
    calculate_hashes_by_stream,
    get_service_hex_main_hash,
    get_service_list_of_hashes,
)
from protos.celaut_pb2 import Metadata


class CalculateHashesTests(unittest.TestCase):
    CONTENT = b"celaut service content" * 500

    def _by_type(self, hashes):
        return {h.type: h.value for h in hashes}

    def test_records_all_three_algorithms(self):
        hashes = calculate_hashes(self.CONTENT)
        self.assertEqual(
            {h.type for h in hashes}, {SHA3_256_ID, SHAKE_256_ID, BLAKE2B_ID}
        )

    def test_no_duplicate_types(self):
        # What zip_with_dockerfile.py's "Metadata integrity validation" checks
        # for on the whole hashtag.hash list -- these three must never collide.
        hashes = calculate_hashes(self.CONTENT)
        types = [h.type for h in hashes]
        self.assertEqual(len(types), len(set(types)))

    def test_each_digest_matches_hashlib_directly(self):
        by_type = self._by_type(calculate_hashes(self.CONTENT))
        self.assertEqual(by_type[SHA3_256_ID], hashlib.sha3_256(self.CONTENT).digest())
        self.assertEqual(by_type[SHAKE_256_ID], hashlib.shake_256(self.CONTENT).digest(32))
        self.assertEqual(
            by_type[BLAKE2B_ID], hashlib.blake2b(self.CONTENT, digest_size=32).digest()
        )

    def test_streaming_and_whole_bytes_agree(self):
        whole = calculate_hashes(self.CONTENT)
        chunks = [self.CONTENT[:7], self.CONTENT[7:4000], self.CONTENT[4000:]]
        streamed = calculate_hashes_by_stream(iter(chunks))
        self.assertEqual(self._by_type(whole), self._by_type(streamed))

    def test_an_empty_object_still_gets_all_three(self):
        by_type = self._by_type(calculate_hashes(b""))
        self.assertEqual(by_type[SHA3_256_ID], hashlib.sha3_256(b"").digest())
        self.assertEqual(by_type[SHAKE_256_ID], hashlib.shake_256(b"").digest(32))
        self.assertEqual(by_type[BLAKE2B_ID], hashlib.blake2b(b"", digest_size=32).digest())

    def test_get_service_list_of_hashes_includes_blake2b(self):
        hashes = get_service_list_of_hashes(self.CONTENT)
        self.assertIn(BLAKE2B_ID, {h.type for h in hashes})


class MainHashResolutionUnaffectedTests(unittest.TestCase):
    """Adding a BLAKE2B entry must not change which hash resolves as "the" one.

    A default node's `hashing.HASH` is sha3_256, so that entry is what must
    keep winning even with BLAKE2B now sitting alongside it in the same list.
    """

    def test_still_resolves_sha3_first_by_default(self):
        hashes = calculate_hashes(b"content")
        by_type = {h.type: h.value for h in hashes}
        self.assertEqual(
            get_service_hex_main_hash(metadata=Metadata(), other_hashes=hashes),
            by_type[SHA3_256_ID].hex(),
        )


if __name__ == "__main__":
    unittest.main()
