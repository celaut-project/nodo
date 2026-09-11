"""Where a signing backend's receiving address comes from, and what survives a dead node.

Ergo derives its address from the mnemonic on every call, so it stores nothing. Bitcoin
cannot: nodo holds no Bitcoin key on any backend -- with `core` the operator's bitcoind
owns the wallet and nodo never sees a seed at all -- and what makes an address usable is
not that it can be derived but that **Core is watching it**. An address Core does not
watch reports no payments and spends no output.

So Core is asked, under a label, and the answer is cached. The cache is the answer to
the one thing asking cannot survive: a bitcoind that is down while a payment this node
already advertised is arriving.
"""
import os
import tempfile
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.payment_system.contracts.bitcoin.backend import (
        LABEL_NOT_FOUND,
        BackendUnavailable,
        ChainBackend,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    btc = None  # type: ignore[assignment]

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
OTHER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LabelledAddressTests(unittest.TestCase):
    """What Core is asked, so that two calls get the same address."""

    def _core(self, result):
        chain = ChainBackend("http://localhost:8332", wallet="nodo")
        calls = []

        def _call(method, params=None, **_):
            calls.append((method, params))
            if isinstance(result, Exception):
                raise result
            return result

        chain._call = _call  # type: ignore[assignment]
        return chain, calls

    def test_the_label_is_what_the_address_is_found_by(self):
        chain, calls = self._core({ADDRESS: {"purpose": "receive"}})
        self.assertEqual(chain.receive_address(), ADDRESS)
        self.assertEqual(calls, [("getaddressesbylabel", ["nodo"])])

    def test_a_wallet_with_no_such_label_answers_none_rather_than_failing(self):
        """Core refuses the label instead of returning an empty set.

        "No address carries this label" is an answer -- `init` mints one from it --
        while "I could not reach bitcoind" is not, and the two arrive as the same
        exception class.
        """
        chain, _ = self._core(BackendUnavailable("nope", code=LABEL_NOT_FOUND))
        self.assertEqual(chain.receive_address(), "")

    def test_a_node_that_cannot_be_reached_still_raises(self):
        chain, _ = self._core(BackendUnavailable("connection refused"))
        with self.assertRaises(BackendUnavailable):
            chain.receive_address()

    def test_several_labelled_addresses_resolve_the_same_way_every_time(self):
        # Arbitrary, but stable: what matters is that two calls, and two restarts,
        # never pick a different one out of the same wallet.
        chain, _ = self._core({OTHER: {}, ADDRESS: {}})
        self.assertEqual(chain.receive_address(), sorted([OTHER, ADDRESS])[0])

    def test_reading_the_address_never_mints_one(self):
        chain, calls = self._core({ADDRESS: {}})
        chain.receive_address()
        self.assertNotIn("getnewaddress", [method for method, _ in calls])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AddressCacheTests(unittest.TestCase):
    """The file under `__cache__`, and what it is for."""

    def setUp(self):
        self._cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache.cleanup)
        self.path = os.path.join(self._cache.name, btc.RECEIVE_ADDRESS_CACHE_FILE)
        # Module state, not the file: two tests in a row must not see each other's.
        btc._address_reads = 0
        btc._address_on_disk = None
        patched = mock.patch.object(
            btc.env_manager, "get",
            side_effect=lambda key, default=None: (
                self._cache.name if key == "CACHE" else ""
            ),
        )
        patched.start()
        self.addCleanup(patched.stop)

    def _chain(self, address=ADDRESS, fails=False):
        chain = mock.Mock()
        if fails:
            chain.receive_address.side_effect = BackendUnavailable("bitcoind is down")
        else:
            chain.receive_address.return_value = address
        return chain

    def _cached(self):
        with open(self.path) as file:
            return file.read().strip()

    def test_core_is_asked_and_the_answer_is_cached(self):
        with mock.patch.object(btc, "backend", return_value=self._chain()):
            self.assertEqual(btc._address_from_core(), ADDRESS)
        self.assertEqual(self._cached(), ADDRESS)

    def test_a_dead_bitcoind_falls_back_to_what_was_advertised(self):
        """The whole point of the file.

        A node whose bitcoind is down is still owed money by peers who were told an
        address. Failing to name it would reject a payment that is already on-chain.
        """
        with mock.patch.object(btc, "backend", return_value=self._chain()):
            btc._address_from_core()
        with mock.patch.object(btc, "backend", return_value=self._chain(fails=True)):
            self.assertEqual(btc._address_from_core(), ADDRESS)

    def test_a_dead_bitcoind_with_no_cache_has_no_answer_to_give(self):
        with mock.patch.object(btc, "backend", return_value=self._chain(fails=True)):
            self.assertEqual(btc._address_from_core(), "")

    def test_an_empty_wallet_falls_back_to_the_cache_rather_than_to_nothing(self):
        # Core answering "no such label" for a wallet that was replaced must not lose
        # the address this node has already been advertising.
        with mock.patch.object(btc, "backend", return_value=self._chain()):
            btc._address_from_core()
        with mock.patch.object(btc, "backend", return_value=self._chain(address="")):
            self.assertEqual(btc._address_from_core(), ADDRESS)

    def test_a_changed_answer_is_written_through_immediately(self):
        with mock.patch.object(btc, "backend", return_value=self._chain()):
            btc._address_from_core()
        with mock.patch.object(btc, "backend", return_value=self._chain(address=OTHER)):
            btc._address_from_core()
        self.assertEqual(self._cached(), OTHER)

    def test_an_unchanged_answer_is_not_written_on_every_read(self):
        # This runs on the payment path; a file write per validated payment is not
        # what the cache is for.
        chain = self._chain()
        with mock.patch.object(btc, "backend", return_value=chain):
            btc._address_from_core()
            mtime = os.stat(self.path).st_mtime_ns
            for _ in range(btc.ADDRESS_CACHE_REWRITE_EVERY - 2):
                btc._address_from_core()
            self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)

    def test_a_cache_somebody_deleted_comes_back(self):
        """Rewriting periodically is what notices a cleared cache directory.

        Nothing announces that the file is gone, and the read that needs it is the one
        that happens when bitcoind is already down.
        """
        chain = self._chain()
        with mock.patch.object(btc, "backend", return_value=chain):
            btc._address_from_core()
            os.unlink(self.path)
            for _ in range(btc.ADDRESS_CACHE_REWRITE_EVERY):
                btc._address_from_core()
        self.assertEqual(self._cached(), ADDRESS)

    def test_an_unwritable_cache_costs_the_fallback_and_nothing_else(self):
        # Raising here would fail an advertisement, or a payment validation, over a
        # file this node can do without.
        with mock.patch.object(btc, "_address_cache_path",
                               return_value="/proc/nodo/does-not-exist"), \
                mock.patch.object(btc, "backend", return_value=self._chain()):
            self.assertEqual(btc._address_from_core(), ADDRESS)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WalletAddressTests(unittest.TestCase):
    """What the contract as a whole answers, and what it refuses to answer."""

    def setUp(self):
        btc._address_reads = 0
        btc._address_on_disk = None

    def test_a_signing_backend_asks_core_rather_than_reading_configuration(self):
        chain = mock.Mock()
        chain.receive_address.return_value = ADDRESS
        with mock.patch.object(btc, "_signs", return_value=True), \
                mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "_remember_address"), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"):
            self.assertEqual(btc.get_wallet_address(), ADDRESS)
        chain.receive_address.assert_called_once()

    def test_no_address_anywhere_names_both_places_it_looked(self):
        chain = mock.Mock()
        chain.receive_address.return_value = ""
        with mock.patch.object(btc, "_signs", return_value=True), \
                mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "_read_cached_address", return_value=""):
            with self.assertRaises(ValueError) as raised:
                btc.get_wallet_address()
        self.assertIn("label", str(raised.exception))
        self.assertIn(btc.RECEIVE_ADDRESS_CACHE_FILE, str(raised.exception))

    def test_an_address_for_another_network_is_refused_rather_than_advertised(self):
        """A wallet on the wrong chain is a misconfiguration, not an address.

        Advertised, it would have peers pay to a script this node cannot be credited
        for -- and minting another one from the same wallet would not help.
        """
        chain = mock.Mock()
        chain.receive_address.return_value = ADDRESS  # mainnet
        with mock.patch.object(btc, "_signs", return_value=True), \
                mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "_remember_address"), \
                mock.patch.object(btc, "NETWORK", lambda: "testnet"):
            with self.assertRaisesRegex(ValueError, "not a valid testnet"):
                btc.get_wallet_address()
            with self.assertRaisesRegex(ValueError, "not a valid testnet"):
                btc.ensure_receiving_address()
        self.assertFalse(chain.new_address.called)


if __name__ == "__main__":
    unittest.main()
