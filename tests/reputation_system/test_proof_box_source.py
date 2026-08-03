"""Where a proof's boxes are read from, and in which JSON shape.

The reputation validators compare ``box["ergoTree"]`` against the canonical
contract as hex. Reading the boxes through AppKit's ``InputBox.toJson()`` broke
that: it renders the field as the Scala object's ``toString``, so every peer
proof was rejected as "off the canonical contract".
"""
import atexit
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

NODE_URL = "https://node.example"

# Importing the reputation chain pulls in src/utils/logger, which reads STORAGE
# at import time — so the config has to be in place first. Doing it here (rather
# than relying on the repo's config.yaml) keeps the test independent of whatever
# the developer has locally.
_TMPDIR = tempfile.mkdtemp(prefix="nodo-test-proof-box-")
atexit.register(shutil.rmtree, _TMPDIR, ignore_errors=True)

_MODULE_CONFIG = Path(_TMPDIR) / "config.yaml"
_MODULE_CONFIG.write_text(
    f"main:\n  STORAGE: {_TMPDIR}\nledgers:\n  ergo:\n    NODE_URL: {NODE_URL}\n",
    encoding="utf-8",
)

from src.utils.config import ConfigManager  # noqa: E402
from src.utils.singleton import Singleton  # noqa: E402

ConfigManager(config_path=str(_MODULE_CONFIG)).load_config()

from src.reputation_system import envs  # noqa: E402
from src.reputation_system.contracts.ergo import proof_validation  # noqa: E402

CANONICAL_TREE = envs.REPUTATION_PROOF_ERGO_TREE
TYPE_NFT = "64060577c3393e0e3cf8938ec8e6a2002ded27ece17750aa5add7d5c3e1227ba"
PROOF_ID = "66099e54b78fa30d4fced623dd30c34fe4ff8bede78efc397fa05062b3b10fe5"
P2PK_PROPOSITION = "0008cd" + "03" * 33

# What AppKit's InputBox.toJson() puts in the field instead of hex. 25 == 0x19,
# the canonical tree's header byte, so it *is* the same contract — just an
# unusable representation for a hex comparison.
APPKIT_TREE_RENDERING = "ErgoTree(25,ArraySeq(IntConstant(0), IntConstant(0)), ...)"


def _coll_byte(data: bytes) -> str:
    """Serialize bytes as an Ergo Coll[Byte]: 0e + VLQ(len) + payload."""
    n = len(data)
    vlq = b""
    while True:
        chunk = n & 0x7F
        n >>= 7
        vlq += bytes([chunk | 0x80]) if n else bytes([chunk])
        if not n:
            break
    return "0e" + vlq.hex() + data.hex()


def _box(box_id: str = "abc", spent: str = None, ergo_tree: str = None) -> dict:
    """A reputation box in the shape `GET /blockchain/box/byTokenId` returns."""
    box = {
        "boxId": box_id,
        "ergoTree": CANONICAL_TREE if ergo_tree is None else ergo_tree,
        "assets": [{"tokenId": PROOF_ID, "amount": 1000000000}],
        "additionalRegisters": {
            "R4": _coll_byte(bytes.fromhex(TYPE_NFT)),
            "R5": _coll_byte(bytes.fromhex(PROOF_ID)),
            "R7": _coll_byte(bytes.fromhex(P2PK_PROPOSITION)),
        },
    }
    if spent:
        box["spentTransactionId"] = spent
    return box


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class ProofBoxSourceTests(unittest.TestCase):
    def setUp(self):
        # Re-point the singleton at the module config: another test may have
        # replaced it, and _get_unspent_boxes_by_token reads NODE_URL from it.
        Singleton._instances.pop(ConfigManager, None)
        ConfigManager(config_path=str(_MODULE_CONFIG)).load_config()

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def test_boxes_come_from_the_node_json_with_a_hex_ergotree(self):
        with mock.patch("requests.get", return_value=_Response({"items": [_box()]})) as get:
            boxes = proof_validation._get_unspent_boxes_by_token(PROOF_ID)

        self.assertEqual(
            get.call_args[0][0], f"{NODE_URL}/blockchain/box/byTokenId/{PROOF_ID}"
        )
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["ergoTree"], CANONICAL_TREE)

    def test_a_canonical_box_now_passes_validation(self):
        # The end of the chain that was failing: a real peer proof (this is the
        # register layout of the one on chain) must be accepted.
        boxes = [_box()]
        self.assertEqual(proof_validation._boxes_off_canonical_contract(boxes), [])
        self.assertTrue(proof_validation._validate_box_structure(boxes[0]))

    def test_appkit_tostring_rendering_would_be_rejected(self):
        # Regression guard: this is exactly what made every proof look off-contract.
        boxes = [_box(ergo_tree=APPKIT_TREE_RENDERING)]
        self.assertEqual(
            proof_validation._boxes_off_canonical_contract(boxes), [APPKIT_TREE_RENDERING]
        )
        self.assertFalse(proof_validation._validate_box_structure(boxes[0]))

    def test_spent_boxes_are_dropped(self):
        # byTokenId returns the token's whole history; one stale box must not
        # fail an otherwise valid proof.
        payload = {"items": [_box("old", spent="tx1", ergo_tree="dead" * 8), _box("current")]}
        with mock.patch("requests.get", return_value=_Response(payload)):
            boxes = proof_validation._get_unspent_boxes_by_token(PROOF_ID)

        self.assertEqual([b["boxId"] for b in boxes], ["current"])
        self.assertEqual(proof_validation._boxes_off_canonical_contract(boxes), [])

    def test_no_jvm_is_started(self):
        # Validation must not need Java, and must not pay a JVM start per proof.
        def fail(*args, **kwargs):
            raise AssertionError("the JVM must not be involved in reading proof boxes")

        with mock.patch("requests.get", return_value=_Response({"items": [_box()]})), \
                mock.patch.object(proof_validation, "ensure_ergpy_jvm", fail), \
                mock.patch.object(proof_validation, "require_java_module", fail):
            proof_validation._get_unspent_boxes_by_token(PROOF_ID)

    def test_http_error_raises(self):
        with mock.patch("requests.get", return_value=_Response({}, status_code=404)):
            with self.assertRaises(ValueError):
                proof_validation._get_unspent_boxes_by_token(PROOF_ID)

    def test_no_items_returns_empty(self):
        # An unknown token yields no boxes; callers already treat that as a rejection.
        with mock.patch("requests.get", return_value=_Response({"items": []})):
            self.assertEqual(proof_validation._get_unspent_boxes_by_token(PROOF_ID), [])

    def test_missing_node_url_raises(self):
        Singleton._instances.pop(ConfigManager, None)
        config_path = Path(_TMPDIR) / "no-node-url.yaml"
        config_path.write_text(
            f"main:\n  STORAGE: {_TMPDIR}\nledgers:\n  ergo:\n    NODE_URL: ''\n",
            encoding="utf-8",
        )
        ConfigManager(config_path=str(config_path)).load_config()

        with self.assertRaises(ValueError):
            proof_validation._get_unspent_boxes_by_token(PROOF_ID)


if __name__ == "__main__":
    unittest.main()
