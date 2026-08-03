"""An unreachable Ergo node is UNDETERMINED, never "this proof is not yours".

Ownership validation used to collapse both into False, and its callers act
destructively on a False: `sync_reputation_proof_ownership` clears
REPUTATION_PROOF_ID from config.yaml, and the submit path drops the proof id and
mints a brand-new proof — abandoning the reputation accumulated on the old
token. A node outage must not trigger either.
"""
import atexit
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

NODE_URL = "https://node.example"
PROOF_ID = "46bf6503dfa0551e7a74f005f33b717f26115ed21f338297639040d3d0cfe484"
MNEMONIC = "abandon " * 11 + "about"  # shape only; never a real funded seed
OWNER = "0008cd" + "02" * 33
OTHER_OWNER = "0008cd" + "03" * 33

# src/utils/logger.py reads STORAGE at import time, so the config must exist
# before the reputation chain is imported. See test_proof_box_source.
_TMPDIR = tempfile.mkdtemp(prefix="nodo-test-proof-lookup-")
atexit.register(shutil.rmtree, _TMPDIR, ignore_errors=True)


def _config_text(proof_id: str = PROOF_ID, mnemonic: str = MNEMONIC) -> str:
    return (
        f"main:\n  STORAGE: {_TMPDIR}\n"
        "ledgers:\n"
        "  ergo:\n"
        f"    NODE_URL: {NODE_URL}\n"
        f"    WALLET_MNEMONIC: {mnemonic}\n"
        "    reputation:\n"
        f"      REPUTATION_PROOF_ID: '{proof_id}'\n"
    )


_MODULE_CONFIG = Path(_TMPDIR) / "config.yaml"
_MODULE_CONFIG.write_text(_config_text(), encoding="utf-8")

from src.utils.config import ConfigManager  # noqa: E402
from src.utils.singleton import Singleton  # noqa: E402

ConfigManager(config_path=str(_MODULE_CONFIG)).load_config()

from src.reputation_system import envs  # noqa: E402
from src.reputation_system.contracts.ergo import proof_validation  # noqa: E402
from src.reputation_system.contracts.ergo.proof_validation import (  # noqa: E402
    ProofLookupUnavailable,
)

CANONICAL_TREE = envs.REPUTATION_PROOF_ERGO_TREE
TYPE_NFT = "64060577c3393e0e3cf8938ec8e6a2002ded27ece17750aa5add7d5c3e1227ba"


def _coll_byte(data: bytes) -> str:
    n = len(data)
    vlq = b""
    while True:
        chunk = n & 0x7F
        n >>= 7
        vlq += bytes([chunk | 0x80]) if n else bytes([chunk])
        if not n:
            break
    return "0e" + vlq.hex() + data.hex()


def _box(owner: str = OWNER) -> dict:
    return {
        "boxId": "abc",
        "ergoTree": CANONICAL_TREE,
        "assets": [{"tokenId": PROOF_ID, "amount": 1000000000}],
        "additionalRegisters": {
            "R4": _coll_byte(bytes.fromhex(TYPE_NFT)),
            "R5": _coll_byte(bytes.fromhex(PROOF_ID)),
            "R7": _coll_byte(bytes.fromhex(owner)),
        },
    }


class _Response:
    def __init__(self, payload, status_code=200, unreadable=False):
        self._payload = payload
        self.status_code = status_code
        self._unreadable = unreadable

    def json(self):
        if self._unreadable:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _ConfigTestCase(unittest.TestCase):
    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)
        self.config_path = Path(_TMPDIR) / f"{self.id().rsplit('.', 1)[-1]}.yaml"
        self.config_path.write_text(_config_text(), encoding="utf-8")
        ConfigManager(config_path=str(self.config_path)).load_config()

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _stored_proof_id(self):
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return data["ledgers"]["ergo"]["reputation"]["REPUTATION_PROOF_ID"]


class LookupFailureIsUndeterminedTests(_ConfigTestCase):
    def test_unreachable_node_raises_undetermined(self):
        import requests

        with mock.patch("requests.get", side_effect=requests.ConnectionError("no route")):
            with self.assertRaises(ProofLookupUnavailable):
                proof_validation._get_unspent_boxes_by_token(PROOF_ID)

    def test_http_error_raises_undetermined(self):
        with mock.patch("requests.get", return_value=_Response({}, status_code=503)):
            with self.assertRaises(ProofLookupUnavailable):
                proof_validation._get_unspent_boxes_by_token(PROOF_ID)

    def test_unreadable_body_raises_undetermined(self):
        with mock.patch("requests.get", return_value=_Response(None, unreadable=True)):
            with self.assertRaises(ProofLookupUnavailable):
                proof_validation._get_unspent_boxes_by_token(PROOF_ID)

    def test_missing_node_url_raises_undetermined(self):
        Singleton._instances.pop(ConfigManager, None)
        path = Path(_TMPDIR) / "no-node-url.yaml"
        path.write_text(
            f"main:\n  STORAGE: {_TMPDIR}\nledgers:\n  ergo:\n    NODE_URL: ''\n",
            encoding="utf-8",
        )
        ConfigManager(config_path=str(path)).load_config()

        with self.assertRaises(ProofLookupUnavailable):
            proof_validation._get_unspent_boxes_by_token(PROOF_ID)

    def test_stays_a_valueerror_for_existing_callers(self):
        # The function raised ValueError before; keep that contract.
        self.assertTrue(issubclass(ProofLookupUnavailable, ValueError))


class OwnershipValidationTests(_ConfigTestCase):
    """Ownership must propagate UNDETERMINED and still return False on a real no."""

    def _patched_wallet(self, owner: str = OWNER):
        return mock.patch.multiple(
            proof_validation,
            get_public_key=mock.DEFAULT,
            owner_proposition_bytes_hex=mock.MagicMock(return_value=owner),
        )

    def test_undetermined_propagates_instead_of_returning_false(self):
        with self._patched_wallet(), mock.patch.object(
            proof_validation,
            "_get_unspent_boxes_by_token",
            side_effect=ProofLookupUnavailable("node down"),
        ):
            with self.assertRaises(ProofLookupUnavailable):
                proof_validation.validate_reputation_proof_ownership(proof_id=PROOF_ID)

    def test_a_real_negative_still_returns_false(self):
        # Chain answered and the proof belongs to someone else: a verdict, not an outage.
        with self._patched_wallet(owner=OWNER), mock.patch.object(
            proof_validation, "_get_unspent_boxes_by_token", return_value=[_box(OTHER_OWNER)]
        ):
            self.assertFalse(
                proof_validation.validate_reputation_proof_ownership(proof_id=PROOF_ID)
            )

    def test_a_match_still_returns_true(self):
        with self._patched_wallet(owner=OWNER), mock.patch.object(
            proof_validation, "_get_unspent_boxes_by_token", return_value=[_box(OWNER)]
        ):
            self.assertTrue(
                proof_validation.validate_reputation_proof_ownership(proof_id=PROOF_ID)
            )


class SyncKeepsConfigOnOutageTests(_ConfigTestCase):
    def test_outage_leaves_the_proof_id_in_place(self):
        # The regression this PR is about: losing REPUTATION_PROOF_ID to an outage
        # sends the node back to advertising no reputation proof at all.
        with mock.patch.object(
            proof_validation,
            "validate_reputation_proof_ownership",
            side_effect=ProofLookupUnavailable("node down"),
        ):
            self.assertFalse(proof_validation.sync_reputation_proof_ownership())

        self.assertEqual(self._stored_proof_id(), PROOF_ID)
        self.assertEqual(
            ConfigManager().get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), PROOF_ID
        )

    def test_outage_does_not_trigger_the_on_chain_rediscovery(self):
        # Nothing was verified, so there is nothing to reconcile: no scan either.
        with mock.patch.object(
            proof_validation,
            "validate_reputation_proof_ownership",
            side_effect=ProofLookupUnavailable("node down"),
        ), mock.patch.object(
            proof_validation,
            "__find_reputation_proof_id_for_owner",
            side_effect=AssertionError("must not scan the chain when nothing was checked"),
        ):
            self.assertFalse(proof_validation.sync_reputation_proof_ownership())

    def test_a_real_negative_still_clears_the_proof_id(self):
        # Unchanged behaviour: the chain said this proof is not ours.
        with mock.patch.object(
            proof_validation, "validate_reputation_proof_ownership", return_value=False
        ), mock.patch.object(
            proof_validation, "__find_reputation_proof_id_for_owner", return_value=None
        ):
            proof_validation.sync_reputation_proof_ownership()

        self.assertEqual(self._stored_proof_id(), "")

    def test_a_valid_proof_is_left_alone(self):
        with mock.patch.object(
            proof_validation, "validate_reputation_proof_ownership", return_value=True
        ):
            self.assertTrue(proof_validation.sync_reputation_proof_ownership())

        self.assertEqual(self._stored_proof_id(), PROOF_ID)


if __name__ == "__main__":
    unittest.main()
