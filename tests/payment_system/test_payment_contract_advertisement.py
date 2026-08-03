"""What a node advertises as its payment contract must be what a payer can use.

The whole Ergo payment path exchanges the wallet's raw ErgoTree/propositionBytes
as the ``script`` xattr: ``process_payment`` feeds it to
``ergo_contract_from_proposition_bytes`` to build the output box, and
``payment_process_validator`` turns it back into an address to check the payment
landed on the receiving node's wallet. ``local_payment_methods`` was instead
advertising the ErgoScript type string as the script — and crashing on the way
there, taking every GetPeerInfo down with it.
"""
import atexit
import shutil
import tempfile
import unittest
from hashlib import sha3_256
from pathlib import Path
from unittest import mock

import yaml

# The payment chain reads settings while being imported (src/utils/logger.py needs
# STORAGE, sql_connection needs the reputation amounts, …), so a config has to be
# in place first. Build it from config.example.yaml — a partial one would leave
# unrelated modules in the chain holding None.
_TMPDIR = tempfile.mkdtemp(prefix="nodo-test-payment-adv-")
atexit.register(shutil.rmtree, _TMPDIR, ignore_errors=True)

_example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
_example.setdefault("main", {}).update(
    {"STORAGE": _TMPDIR, "DATABASE_FILE": f"{_TMPDIR}/database.sqlite"}
)
# Keep load_config side-effect free: no free-port scan, no generated mnemonic.
_example.setdefault("network", {})["GATEWAY_PORT"] = 4040
for _ledger in (_example.get("ledgers") or {}).values():
    if isinstance(_ledger, dict) and _ledger.get("WALLET_MNEMONIC") == "auto":
        _ledger["WALLET_MNEMONIC"] = ""

_CONFIG = Path(_TMPDIR) / "config.yaml"
_CONFIG.write_text(yaml.safe_dump(_example, indent=2), encoding="utf-8")

from src.utils.config import ConfigManager  # noqa: E402
from src.utils.singleton import Singleton  # noqa: E402

_saved_manager = Singleton._instances.pop(ConfigManager, None)
ConfigManager(config_path=str(_CONFIG)).load_config()
try:
    from protos import celaut_pb2 as celaut  # noqa: E402
    from src.payment_system import ledgers  # noqa: E402
    from src.utils.contract_xattrs import (  # noqa: E402
        get_address,
        get_contract_type,
        get_script,
        get_token_id,
    )
finally:
    Singleton._instances.pop(ConfigManager, None)
    if _saved_manager is not None:
        Singleton._instances[ConfigManager] = _saved_manager

# A real P2PK ErgoTree: 0008cd + 33-byte compressed pubkey. Not UTF-8 decodable,
# which is exactly what used to crash local_payment_methods.
PROPOSITION_BYTES = bytes.fromhex("0008cd03927647d5fab8e2e718601177a3528468fc97b9a495be1b7e" + "00" * 8)
ERGO_LEDGER = celaut.Contract.Ledger(tags=["ergo"], prose="Ergo", formal=b"")


class PaymentContractAdvertisementTests(unittest.TestCase):
    def _advertised(self, instances):
        with mock.patch.object(
            ledgers, "get_peer_contract_instances", return_value=iter(instances)
        ):
            return list(ledgers.local_payment_methods())

    def test_binary_proposition_bytes_do_not_crash(self):
        # The regression: `script.decode("utf-8")` raised UnicodeDecodeError on
        # every call, so a node with a wallet could not answer GetPeerInfo at all.
        methods = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        self.assertEqual(len(methods), 1)

    def test_the_script_travels_untouched(self):
        [method] = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        self.assertEqual(get_script(method.contract), PROPOSITION_BYTES)

    def test_the_type_string_is_not_advertised_as_the_script(self):
        # It used to be, which would have made a paying peer build the output box
        # from b"proveDlog(decodePoint())" instead of the wallet's ErgoTree.
        [method] = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        self.assertNotEqual(get_script(method.contract), ledgers.CONTRACT.encode("utf-8"))

    def test_contract_type_carries_the_stable_identity(self):
        # add_contract keys on sha3(contract_type), so the receiving peer must
        # derive the same CONTRACT_HASH this node looks its instances up by.
        [method] = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        contract_type = get_contract_type(method.contract)
        self.assertEqual(contract_type, ledgers.CONTRACT.encode("utf-8"))
        self.assertEqual(sha3_256(contract_type).hexdigest(), ledgers.CONTRACT_HASH)

    def test_no_textual_address_is_advertised(self):
        # A readable address is derived at the AppKit boundary, never exchanged.
        [method] = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        self.assertEqual(get_address(method.contract), "")

    def test_ledger_and_token_are_preserved(self):
        [method] = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        self.assertEqual(list(method.contract.ledger.tags), ["ergo"])
        self.assertEqual(get_token_id(method.contract), "ERG")

    def test_every_instance_is_advertised(self):
        other = bytes.fromhex("0008cd02" + "11" * 32)
        methods = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER), (other, ERGO_LEDGER)])
        self.assertEqual(
            [get_script(m.contract) for m in methods], [PROPOSITION_BYTES, other]
        )

    def test_no_instances_advertises_nothing(self):
        self.assertEqual(self._advertised([]), [])


if __name__ == "__main__":
    unittest.main()
