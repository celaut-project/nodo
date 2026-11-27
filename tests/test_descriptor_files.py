"""The descriptor files of contract/descriptors/ follow contract/descriptors/REASON.md."""
import os
import re
import unittest

from src.utils import descriptor_file as df

ROOT = df.ROOT
# REASON rule 6.4: these files came before the rules of the formal. Their bytes are on
# the wire, so they have no class= and no name= line until the code follows the contract.
LEGACY = {
    "contracts/payment/bitcoin-p2wpkh.descriptor",
    "contracts/payment/ergo-p2pk.descriptor",
    "policies/api_slot/access.descriptor",
    "policies/api_slot/tunnel_auth.descriptor",
    "policies/api_slot/wake.descriptor",
    "policies/filesystem_entry/share.descriptor",
    "policies/filesystem_root/entry_metadata.descriptor",
    "policies/filesystem_root/read_mode.descriptor",
    # The formals that the code builds today (#693): the files copy their bytes.
    "ledgers/bitcoin-mainnet.descriptor",
    "ledgers/ergo-mainnet.descriptor",
    "protocols/address/bee-rpc.descriptor",
    "protocols/address/celaut-gateway.descriptor",
    "protocols/address/grpc.descriptor",
    "protocols/address/http2.descriptor",
    "protocols/address/tcp.descriptor",
    "protocols/address/tls.descriptor",
    "protocols/address/tor-onion-v3.descriptor",
    "signatures/schemes/ed25519.descriptor",
}
# The formals that do not pass the test of the probe yet (contract/PENDING.md, section 2)
# because an id of their formal is the digest of bytes that no file holds. None now: the
# benchmarks name their tool as documents (#695).
NOT_IN_USES = set()
KEY = re.compile(r"^[A-Za-z0-9_.+-]+$")


def _lines(formal: bytes):
    return formal.decode("utf-8").split("\n")


class FileFormatTests(unittest.TestCase):

    def test_each_file_has_the_format_of_a_descriptor_file(self):
        self.assertTrue(df.files(), "no descriptor file found")
        for rel in df.files():
            with self.subTest(rel):
                df.read(os.path.join(ROOT, rel))

    def test_the_reader_refuses_a_file_without_one_marker(self):
        for data in (
            b"tags: a\n\nprose\n",
            b"tags: a\n\n=== formal\nx=1",
            b"prose\n\n=== formal\nx=1\n",
            b"tags: a\r\n\n=== formal\nx=1\n",
            b"tags: a\n\n=== formal\n\n",
        ):
            with self.subTest(data):
                with self.assertRaises(df.DescriptorFileError):
                    df.parse(data)

    def test_the_formal_is_the_bytes_after_the_marker(self):
        d = df.parse(b"tags: memory, ram\n\nThe memory.\n\n=== formal\nclass=resource\nname=memory\n")
        self.assertEqual(d.tags, ["memory", "ram"])
        self.assertEqual(d.prose, "The memory.")
        self.assertEqual(d.formal, b"class=resource\nname=memory")

    def test_the_formal_of_a_document_can_hold_any_byte(self):
        d = df.parse(b"tags: a\n\nA file.\n\n=== formal\nline\r\n\n=== formal\n\x00\n\n")
        self.assertEqual(d.formal, b"line\r\n\n=== formal\n\x00\n")


class FormalRulesTests(unittest.TestCase):

    def test_the_lines_are_sorted_keys_with_no_repeat(self):
        for rel in df.files():
            if df.is_document(rel):
                continue
            with self.subTest(rel):
                self.assertNotIn(b"\r", df.read(os.path.join(ROOT, rel)).formal)
                lines = _lines(df.read(os.path.join(ROOT, rel)).formal)
                keys = []
                for line in lines:
                    key, sep, _ = line.partition("=")
                    self.assertEqual(sep, "=", f"not a key=value line: {line!r}")
                    self.assertRegex(key, KEY)
                    keys.append(key.encode())
                self.assertEqual(keys, sorted(set(keys)))

    def test_each_file_is_in_a_folder_of_its_class(self):
        for rel in df.files():
            folder = os.path.dirname(rel)
            with self.subTest(rel):
                self.assertIn(folder, df.folders(), f"{folder} has no {df.FOLDER_RULES}")
                self.assertIsNotNone(df.folder_class(folder), f"{folder} gives no class")

    def test_a_new_file_has_its_class_and_its_name(self):
        for rel in df.files():
            if rel in LEGACY or df.is_document(rel):
                continue
            with self.subTest(rel):
                pairs = dict(
                    line.split("=", 1) for line in _lines(df.read(os.path.join(ROOT, rel)).formal)
                )
                self.assertEqual(pairs.get("class"), df.folder_class(os.path.dirname(rel)))
                self.assertEqual(os.path.basename(rel), f"{pairs.get('name')}{df.SUFFIX}")

    def test_the_legacy_files_exist(self):
        self.assertLessEqual(LEGACY, set(df.files()))

    def test_each_folder_gives_a_known_class_or_is_a_group(self):
        classes = set(re.findall(r"^\| `([a-z-]+)` \|", open(
            os.path.join(ROOT, "REASON.md"), encoding="utf-8").read(), re.MULTILINE))
        self.assertTrue(classes)
        for folder in df.folders():
            cls = df.folder_class(folder)
            if cls is not None:
                with self.subTest(folder):
                    self.assertIn(cls, classes)

    def test_two_files_have_different_formals(self):
        seen = {}
        for rel in df.files():
            formal = df.read(os.path.join(ROOT, rel)).formal
            self.assertNotIn(formal, seen, f"{rel} has the formal of {seen.get(formal)}")
            seen[formal] = rel

    def test_each_reference_resolves_to_a_file(self):
        # REASON 2.9: Descriptor.uses holds the formal of each id in the formal. So each id
        # must be the digest of a formal of this folder, with the algorithm of its type.
        for rel in df.files():
            if df.is_document(rel) or rel in NOT_IN_USES:
                continue
            with self.subTest(rel):
                df.uses(df.read(os.path.join(ROOT, rel)).formal)

    def test_the_exceptions_still_fail(self):
        for rel in NOT_IN_USES:
            with self.subTest(rel):
                with self.assertRaises(KeyError):
                    df.uses(df.read(os.path.join(ROOT, rel)).formal)

    def test_uses_follows_the_first_occurrence(self):
        a, b = df.formal_id(b"a"), df.formal_id(b"b")
        self.assertEqual(df.references(f"x={b} {a}\ny={a}".encode()), [b, a])


class IndexTests(unittest.TestCase):

    def test_the_index_is_current(self):
        with open(os.path.join(ROOT, df.INDEX), encoding="utf-8") as f:
            self.assertEqual(
                df.generated_part(f.read()), df.render_generated(),
                "run: python -m src.utils.descriptor_file",
            )

    def test_the_type_of_an_id_is_the_digest_of_the_empty_input(self):
        fid = df.formal_id(b"x")
        self.assertTrue(fid.startswith(df.hash_type() + ":"))
        self.assertEqual(len(fid.split(":")[1]), 64)


if __name__ == "__main__":
    unittest.main()
