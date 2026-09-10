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
BITCOIN_LEDGER = celaut.Contract.Ledger(tags=["bitcoin"], prose="Bitcoin", formal=b"")

# The stable, wallet-independent type string Ergo's contract is identified by. Stated
# here rather than imported: the point of the type is that it is a fixed value both
# sides derive the same hash from, so a test that read it from the code under test
# could not notice it changing.
ERGO_CONTRACT = "proveDlog(decodePoint())"
ERGO_CONTRACT_HASH = sha3_256(ERGO_CONTRACT.encode("utf-8")).hexdigest()


def _contract(contract=ERGO_CONTRACT, ledger="ergo", asset="ERG", rate=1_000_000_000,
              is_demo=False):
    """One registered payment contract, as `local_payment_methods` reads it."""
    module = mock.Mock()
    module.CONTRACT = contract
    module.CONTRACT_HASH = sha3_256(contract.encode("utf-8")).hexdigest()
    module.LEDGER = ledger
    module.NATIVE_ASSET = asset
    module.is_demo = is_demo
    module.mu_per_unit.return_value = rate
    return module


def advertised(instances, offered=None):
    """What this node advertises, given a registry and the rows it has stored.

    The registry is patched rather than taken from the environment: what a node offers
    depends on which ledgers are configured and which runtimes are present, and this is
    a test about the shape of the advertisement.

    ``instances`` rows are ``(script, ledger)``, optionally with a third element naming
    which contract stored them -- so one call can give two contracts a row each.
    """
    offered = offered if offered is not None else [_contract()]
    registry = {module.CONTRACT_HASH: module for module in offered}
    by_hash = {}
    for module in offered:
        by_hash[module.CONTRACT_HASH] = [
            row for row in instances
            if len(row) < 3 or row[2] == module.CONTRACT_HASH
        ]

    def rows(contract_hash, peer_id="LOCAL"):
        return iter([row[:2] for row in by_hash.get(contract_hash, [])])

    with mock.patch(
        "src.payment_system.contracts.registry.contracts", return_value=registry
    ), mock.patch.object(ledgers, "get_peer_contract_instances", side_effect=rows):
        return list(ledgers.local_payment_methods())


class PaymentContractAdvertisementTests(unittest.TestCase):
    def _advertised(self, instances, offered=None):
        return advertised(instances, offered)

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
        self.assertNotEqual(get_script(method.contract), ERGO_CONTRACT.encode("utf-8"))

    def test_contract_type_carries_the_stable_identity(self):
        # add_contract keys on sha3(contract_type), so the receiving peer must
        # derive the same CONTRACT_HASH this node looks its instances up by.
        [method] = self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER)])
        contract_type = get_contract_type(method.contract)
        self.assertEqual(contract_type, ERGO_CONTRACT.encode("utf-8"))
        self.assertEqual(sha3_256(contract_type).hexdigest(), ERGO_CONTRACT_HASH)

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



class MultipleContractAdvertisementTests(unittest.TestCase):
    """A node advertises every contract it can settle through, not just Ergo's.

    This is what "nodo allows the simultaneous use of multiple ledgers"
    (`docs/ERGO.md`, first line) means on the wire, and until the registry existed it
    was aspiration: `local_payment_methods` named Ergo literally and yielded its rate.
    """

    def _advertised(self, instances, offered):
        return advertised(instances, offered)

    def test_each_contract_carries_its_own_asset_and_rate(self):
        ergo = _contract()
        bitcoin = _contract(contract="p2wpkh", ledger="bitcoin", asset="BTC",
                            rate=200_000_000_000_000)
        script = bytes.fromhex("0014" + "22" * 20)
        methods = self._advertised(
            [
                (PROPOSITION_BYTES, ERGO_LEDGER, ergo.CONTRACT_HASH),
                (script, BITCOIN_LEDGER, bitcoin.CONTRACT_HASH),
            ],
            [ergo, bitcoin],
        )

        by_asset = {get_token_id(method.contract): method for method in methods}
        self.assertEqual(set(by_asset), {"ERG", "BTC"})
        self.assertEqual(int(by_asset["ERG"].mu_per_unit.n), 1_000_000_000)
        self.assertEqual(int(by_asset["BTC"].mu_per_unit.n), 200_000_000_000_000)
        # Each one's own script, untouched, and its own ledger tag.
        self.assertEqual(get_script(by_asset["BTC"].contract), script)
        self.assertEqual(list(by_asset["BTC"].contract.ledger.tags), ["bitcoin"])

    def test_a_contract_that_settles_on_no_chain_is_never_advertised(self):
        # A peer that read the simulated contract out of GetPeerInfo and paid through
        # it would have paid into nothing.
        demo = _contract(contract="simulated", ledger="simulated", asset="", is_demo=True)
        methods = self._advertised(
            [(PROPOSITION_BYTES, ERGO_LEDGER, demo.CONTRACT_HASH)], [demo]
        )
        self.assertEqual(methods, [])

    def test_a_contract_whose_rate_cannot_be_read_is_not_advertised(self):
        """Better silent than priced by guesswork.

        `mu_per_unit` is the only thing that makes a price quoted in MU actionable to
        the peer reading it. Advertised without one -- or with zero -- the peer converts
        this node's prices by dividing by nothing.
        """
        broken = _contract()
        broken.mu_per_unit.side_effect = ValueError("MU_PER_NANOERG is not a number")
        self.assertEqual(
            self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER, broken.CONTRACT_HASH)],
                             [broken]),
            [],
        )

        zero = _contract(rate=0)
        self.assertEqual(
            self._advertised([(PROPOSITION_BYTES, ERGO_LEDGER, zero.CONTRACT_HASH)],
                             [zero]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
