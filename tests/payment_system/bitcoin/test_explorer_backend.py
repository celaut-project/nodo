"""Reaching Bitcoin over a public API while signing locally, like Ergo.

The explorer answers questions about the chain and relays what it is handed; the key is
derived from the mnemonic and every transaction is built and signed here. So the tests
that matter are the ones about what crosses that boundary: the transaction that is posted,
the outputs it is allowed to spend, and what is believed about the answer.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import explorer, signer
    from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
    from src.utils.bitcoin_units import script_pubkey_from_address
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    explorer = None  # type: ignore[assignment]

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
OTHER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon about"
)
# The first BIP-84 address for MNEMONIC.
OWN = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


def _wallet(network="mainnet"):
    return signer.derive_wallet_key(MNEMONIC, "", network)


def _backend(responses, posts=None, post_answer=None):
    """An explorer backend whose HTTP is answered from ``responses``.

    Reads come from the dict by path; writes are appended to ``posts`` and answered with
    ``post_answer`` (the txid of what was posted, when it is not given).
    """
    chain = explorer.ExplorerBackend("https://example.invalid/api", wallet=_wallet())
    chain._get = lambda path: responses.get(path)  # type: ignore[assignment]

    def _post(path, body):
        if posts is not None:
            posts.append((path, body))
        return post_answer if post_answer is not None else _txid_of(body)

    chain._post = _post  # type: ignore[assignment]
    return chain


def _txid_of(raw_hex):
    import hashlib

    raw = bytes.fromhex(raw_hex)
    # Strip marker, flag and witnesses to the legacy serialisation the txid hashes.
    from tests.payment_system.bitcoin.test_signer import parse

    inputs, outputs, witnesses = parse(raw_hex)
    body = raw[:4] + raw[6:-4 - sum(
        1 + sum(len(signer._varint(len(i))) + len(i) for i in stack) for stack in witnesses
    )] + raw[-4:]
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()[::-1].hex()


def _utxo(txid_byte, value, height=900_000, vout=0):
    return {
        "txid": f"{txid_byte:02x}" * 32, "vout": vout, "value": value,
        "status": {"confirmed": height is not None, **(
            {"block_height": height} if height is not None else {}
        )},
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WalletTests(unittest.TestCase):

    def test_it_can_pay_because_it_holds_a_key(self):
        self.assertTrue(_backend({}).can_pay)

    def test_the_address_is_derived_and_never_minted(self):
        chain = _backend({})
        self.assertEqual(chain.receive_address(), OWN)
        # Asking to mint is asking for the same address: the advertised script is one
        # fixed scriptPubKey, and another index would strand payments aimed at the first.
        self.assertEqual(chain.new_address(), OWN)

    def test_the_balance_is_the_wallets_confirmed_outputs(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            f"/address/{OWN}/utxo": [
                _utxo(1, 100_000), _utxo(2, 50_000, height=900_005),
                # Unconfirmed money is not a balance, and must not start being one.
                _utxo(3, 999_999, height=None),
            ],
        })
        self.assertEqual(chain.get_balance(), 150_000)

    def test_a_deeper_threshold_than_confirmed_is_honoured(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            f"/address/{OWN}/utxo": [_utxo(1, 100_000, height=900_000),
                                     _utxo(2, 50_000, height=900_009)],
        })
        self.assertEqual(chain.get_balance(min_conf=3), 100_000)

    def test_a_tip_it_could_not_read_leaves_nothing_spendable(self):
        # "I could not tell" must never read as "deep enough".
        chain = _backend({
            "/blocks/tip/height": None,
            f"/address/{OWN}/utxo": [_utxo(1, 100_000)],
        })
        self.assertEqual(chain.get_balance(), 0)

    def test_an_empty_wallet_has_no_balance(self):
        self.assertEqual(_backend({"/blocks/tip/height": 900_010}).get_balance(), 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SendTests(unittest.TestCase):

    def _funded(self, *utxos, **kwargs):
        posts = []
        chain = _backend({
            "/blocks/tip/height": 900_010,
            f"/address/{OWN}/utxo": list(utxos) or [_utxo(1, 200_000)],
        }, posts, **kwargs)
        return chain, posts

    def test_it_signs_locally_and_relays_the_raw_transaction(self):
        chain, posts = self._funded()
        txid = chain.send_to(ADDRESS, 30_000, fee_rate_sat_vb=5.0, op_return=b"token-1")
        [(path, body)] = posts
        self.assertEqual(path, "/tx")
        self.assertEqual(txid, _txid_of(body))
        from tests.payment_system.bitcoin.test_signer import parse
        inputs, outputs, [witness] = parse(body)
        self.assertEqual(inputs, [("01" * 32, 0)])
        self.assertEqual(outputs[0], (30_000, script_pubkey_from_address(ADDRESS)))
        self.assertEqual(outputs[1], (0, b"\x6a\x07token-1"))
        self.assertEqual(outputs[2][1], _wallet().script_pubkey)  # change, to itself
        self.assertEqual(witness[1], _wallet().public_key)

    def test_unconfirmed_outputs_are_never_spent(self):
        chain, posts = self._funded(_utxo(1, 999_999, height=None), _utxo(2, 100_000))
        chain.send_to(ADDRESS, 30_000, fee_rate_sat_vb=5.0)
        from tests.payment_system.bitcoin.test_signer import parse
        inputs, _, _ = parse(posts[0][1])
        self.assertEqual(inputs, [("02" * 32, 0)])

    def test_one_transaction_pays_every_wallet_in_a_split(self):
        chain, posts = self._funded()
        chain.send_many([(ADDRESS, 30_000), (OTHER, 20_000)], fee_rate_sat_vb=5.0)
        self.assertEqual(len(posts), 1)

    def test_a_sweep_subtracts_the_fee_from_the_amount(self):
        chain, posts = self._funded(_utxo(1, 1_000_000))
        chain.send_to(OTHER, 600_000, fee_rate_sat_vb=5.0, subtract_fee_from_amount=True)
        from tests.payment_system.bitcoin.test_signer import parse
        _, outputs, _ = parse(posts[0][1])
        self.assertLess(outputs[0][0], 600_000)
        self.assertEqual(outputs[1], (400_000, _wallet().script_pubkey))

    def test_insufficient_funds_sends_nothing_and_says_so(self):
        chain, posts = self._funded(_utxo(1, 10_000))
        with self.assertRaises(BackendUnavailable) as raised:
            chain.send_to(ADDRESS, 30_000, fee_rate_sat_vb=5.0)
        self.assertIn("insufficient funds", str(raised.exception))
        self.assertEqual(posts, [])

    def test_an_address_for_another_network_sends_nothing(self):
        chain, posts = self._funded()
        with self.assertRaises(BackendUnavailable) as raised:
            chain.send_to("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", 30_000,
                          fee_rate_sat_vb=5.0)
        self.assertIn("not a valid mainnet address", str(raised.exception))
        self.assertEqual(posts, [])

    def test_a_refused_broadcast_is_an_error_carrying_the_reason(self):
        chain, _ = self._funded()

        def refuse(path, body):
            raise BackendUnavailable("explorer /tx: HTTP 400 min relay fee not met")

        chain._post = refuse  # type: ignore[assignment]
        with self.assertRaisesRegex(BackendUnavailable, "min relay fee"):
            chain.send_to(ADDRESS, 30_000, fee_rate_sat_vb=5.0)

    def test_a_txid_the_explorer_disagrees_about_is_not_a_success(self):
        """What was recorded must be what the explorer says, or it is not recorded."""
        chain, _ = self._funded(post_answer="ab" * 32)
        with self.assertRaisesRegex(BackendUnavailable, "answered"):
            chain.send_to(ADDRESS, 30_000, fee_rate_sat_vb=5.0)

    def test_the_post_error_repeats_a_bounded_excerpt_of_the_servers_prose(self):
        class Response:
            status_code = 400
            text = "x" * 5_000

        chain = explorer.ExplorerBackend("https://example.invalid/api", wallet=_wallet())
        with mock.patch.object(explorer.requests, "post", return_value=Response()):
            with self.assertRaises(BackendUnavailable) as raised:
                chain._post("/tx", "00")
        self.assertLess(len(str(raised.exception)), explorer.ERROR_EXCERPT + 100)

    def test_nothing_to_send_is_refused(self):
        with self.assertRaises(BackendUnavailable):
            self._funded()[0].send_many([], fee_rate_sat_vb=5.0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HistoryTests(unittest.TestCase):

    def _history(self, transactions):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            f"/address/{OWN}/txs": transactions,
        })
        return chain.list_transactions(10)

    def test_a_payment_to_the_wallet_is_a_receive(self):
        [row] = self._history([{
            "txid": "in", "status": {"confirmed": True, "block_height": 900_000,
                                      "block_time": 1_700_000_000},
            "vin": [{"prevout": {"scriptpubkey_address": ADDRESS, "value": 90_000}}],
            "vout": [{"scriptpubkey_address": OWN, "value": 60_000},
                     {"scriptpubkey_address": ADDRESS, "value": 29_000}],
        }])
        self.assertEqual(row["category"], "receive")
        self.assertEqual(row["amount"], 0.0006)
        self.assertEqual(row["confirmations"], 11)

    def test_a_payment_the_wallet_made_is_a_send_for_what_left_it(self):
        [row] = self._history([{
            "txid": "out", "status": {"confirmed": False},
            "vin": [{"prevout": {"scriptpubkey_address": OWN, "value": 200_000}}],
            "vout": [{"scriptpubkey_address": ADDRESS, "value": 30_000},
                     {"scriptpubkey_type": "op_return", "value": 0},
                     {"scriptpubkey_address": OWN, "value": 169_000}],
        }])
        # What went to somebody else: not the fee, not the change, not the OP_RETURN.
        self.assertEqual(row["category"], "send")
        self.assertEqual(row["amount"], -0.0003)
        self.assertEqual(row["address"], ADDRESS)
        self.assertEqual(row["confirmations"], 0)

    def test_a_consolidation_that_pays_nobody_else_is_not_a_payment(self):
        self.assertEqual(self._history([{
            "txid": "self", "status": {"confirmed": True, "block_height": 1},
            "vin": [{"prevout": {"scriptpubkey_address": OWN, "value": 100_000}}],
            "vout": [{"scriptpubkey_address": OWN, "value": 99_000}],
        }]), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfirmationTests(unittest.TestCase):
    """Esplora reports the block a transaction landed in, not how deep it is."""

    def test_depth_is_derived_against_the_tip(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            "/tx/tx-1": {"status": {"confirmed": True, "block_height": 900_000}},
        })
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 11)

    def test_unconfirmed_is_zero_not_missing(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            "/tx/tx-1": {"status": {"confirmed": False}},
        })
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 0)

    def test_a_tip_it_could_not_read_is_zero_confirmations(self):
        """"I could not tell" must never read as "deep enough".

        Answering with a depth derived from a missing tip would credit a payment this
        node cannot see the finality of.
        """
        chain = _backend({
            "/blocks/tip/height": None,
            "/tx/tx-1": {"status": {"confirmed": True, "block_height": 900_000}},
        })
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 0)

    def test_a_transaction_that_is_gone_is_zero_rather_than_an_error(self):
        # Esplora stops reporting a transaction that left the chain; the payer's wait
        # treats that as "not yet" and its own timeout bounds it.
        chain = _backend({"/blocks/tip/height": 900_010, "/tx/tx-1": None})
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 0)

    def test_only_deep_enough_transactions_are_listed_as_received(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            f"/address/{ADDRESS}/txs": [
                {"txid": "deep", "status": {"confirmed": True, "block_height": 900_000}},
                {"txid": "shallow", "status": {"confirmed": True, "block_height": 900_010}},
                {"txid": "mempool", "status": {"confirmed": False}},
            ],
        })
        self.assertEqual(chain.list_received(ADDRESS, 3), [{"txids": ["deep"]}])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NormalisedOutputTests(unittest.TestCase):
    """The contract reads one shape, whichever backend produced it."""

    def test_outputs_carry_satoshi_and_the_raw_script(self):
        chain = _backend({
            "/tx/tx-1": {"vout": [
                {"scriptpubkey": "0014" + "11" * 20, "value": 1_500,
                 "scriptpubkey_type": "v0_p2wpkh"},
            ]}
        })
        [output] = chain.raw_transaction("tx-1")["outputs"]
        self.assertEqual(output["value_sat"], 1_500)
        self.assertEqual(output["script_hex"], "0014" + "11" * 20)
        self.assertIsNone(output["op_return"])

    def test_an_op_return_payload_is_read_out_of_the_raw_script(self):
        # Core reports an OP_RETURN as an `asm` string and Esplora as a hex script, so
        # the payload is decoded here rather than in the contract.
        token = b"deposit-token-1"
        script = "6a" + f"{len(token):02x}" + token.hex()
        chain = _backend({
            "/tx/tx-1": {"vout": [
                {"scriptpubkey": script, "value": 0, "scriptpubkey_type": "op_return"},
            ]}
        })
        [output] = chain.raw_transaction("tx-1")["outputs"]
        self.assertEqual(output["op_return"], token)

    def test_a_pushdata_op_return_is_read_too(self):
        token = b"x" * 80
        script = "6a4c" + f"{len(token):02x}" + token.hex()
        self.assertEqual(explorer._op_return_payload(script), token)

    def test_a_script_that_is_not_an_op_return_carries_no_payload(self):
        self.assertIsNone(explorer._op_return_payload("0014" + "11" * 20))
        self.assertIsNone(explorer._op_return_payload("not-hex"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FeeEstimateTests(unittest.TestCase):

    def test_the_nearest_target_at_or_above_the_one_asked_for_is_used(self):
        # A lower target would promise a confirmation nobody estimated.
        chain = _backend({"/fee-estimates": {"1": 20.0, "3": 8.0, "6": 5.0, "25": 2.0}})
        self.assertEqual(chain.estimate_fee_rate(6), 5.0)
        self.assertEqual(chain.estimate_fee_rate(4), 5.0)
        self.assertEqual(chain.estimate_fee_rate(1), 20.0)

    def test_no_estimate_is_none_rather_than_a_guess(self):
        self.assertIsNone(_backend({"/fee-estimates": None}).estimate_fee_rate(6))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigurationTests(unittest.TestCase):

    def _values(self, **overrides):
        values = {
            "ledgers.bitcoin.EXPLORER_URL": "https://example.invalid/api",
            "ledgers.bitcoin.NETWORK": "mainnet",
            "ledgers.bitcoin.WALLET_MNEMONIC": MNEMONIC,
            "ledgers.bitcoin.WALLET_PASSPHRASE": "",
        }
        values.update(overrides)
        return values

    def _patched(self, values):
        return mock.patch.object(
            explorer.ConfigManager(), "get",
            side_effect=lambda key, default=None: values.get(key, default),
        )

    def _reason(self, **overrides):
        with self._patched(self._values(**overrides)):
            return explorer.configuration_reason()

    def test_a_configured_backend_is_usable(self):
        self.assertIsNone(self._reason())

    def test_no_url_is_named(self):
        self.assertIn("EXPLORER_URL", self._reason(**{"ledgers.bitcoin.EXPLORER_URL": ""}))

    def test_no_cold_wallet_is_needed_to_be_offered(self):
        # Payers are sent to the node's own wallet; the cold wallet is only a sweep target.
        self.assertIsNone(self._reason(**{"ledgers.bitcoin.payments.COLD_WALLET": ""}))

    def test_an_empty_mnemonic_is_named(self):
        self.assertIn("WALLET_MNEMONIC", self._reason(**{"ledgers.bitcoin.WALLET_MNEMONIC": ""}))

    def test_an_invalid_mnemonic_is_named_without_repeating_it(self):
        reason = self._reason(**{"ledgers.bitcoin.WALLET_MNEMONIC": "twelve words that are not real words ok"})
        self.assertIn("WALLET_MNEMONIC", reason)
        self.assertNotIn("twelve", reason)

    def test_the_network_decides_the_address(self):
        with self._patched(self._values(**{"ledgers.bitcoin.NETWORK": "testnet"})):
            self.assertTrue(explorer.backend().address.startswith("tb1"))
        with self._patched(self._values()):
            self.assertEqual(explorer.backend().address, OWN)

    def test_the_passphrase_is_part_of_the_wallet(self):
        with self._patched(self._values(**{"ledgers.bitcoin.WALLET_PASSPHRASE": "hunter2"})):
            self.assertNotEqual(explorer.backend().address, OWN)

    def test_a_backend_cannot_be_built_without_a_url(self):
        with self._patched(self._values(**{"ledgers.bitcoin.EXPLORER_URL": ""})):
            with self.assertRaises(BackendUnavailable):
                explorer.backend()


if __name__ == "__main__":
    unittest.main()
