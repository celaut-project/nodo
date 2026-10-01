"""Signing Bitcoin locally: the key, the transaction, and the ways it can go wrong.

Everything here moves money, so the tests that matter are the ones pinned to something
outside this repository: the BIP-32 and BIP-84 published vectors for the key, and a
transaction built by this code and verified under an independent implementation
(embit's BIP-143 sighash) when it was generated. The rest checks the arithmetic around
them -- which outputs are spent, what the fee is, where change goes.
"""
import unittest

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import signer
    from src.utils.bitcoin_units import script_pubkey_for_address
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    signer = None  # type: ignore[assignment]

ABANDON = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon about"
)
OTHER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
LEGACY = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"

# Built from ABANDON with two UTXOs and verified under embit's BIP-143 sighash.
GOLDEN_HEX = (
    "0200000000010122222222222222222222222222222222222222222222222222222222222222220100"
    "000000fdffffff0330750000000000002200201863143c14c5166804bd19203356da136c985678cd4d"
    "27a1b8c63296049032620000000000000000116a0f6465706f7369742d746f6b656e2d31115c010000"
    "000000160014c0cebcd6c3d3ca8c75dc5ec62ebe55330ef910e202483045022100df9fb92024330982"
    "3b73441dbc78edc3580d2df97a3cfb3a2bb42451974853aa02205c30c1c76b2b9f2d8cf3b4652c27e6"
    "9da2ad04432ed396435ac40c8ebc8d491f01210330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5"
    "f0753bf5beef9c2d91af3c00000000"
)
GOLDEN_TXID = "b88069f91f2b30e010439bd902c86833e1cb8b899cfa5745e2e07cefeb927e2e"


def _read_varint(raw, at):
    first = raw[at]
    if first < 0xFD:
        return first, at + 1
    width = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
    return int.from_bytes(raw[at + 1:at + 1 + width], "little"), at + 1 + width


def parse(tx_hex):
    """A segwit transaction as ``(inputs, outputs, witnesses)`` -- just enough to assert on."""
    raw = bytes.fromhex(tx_hex)
    assert raw[4:6] == b"\x00\x01", "not a segwit serialisation"
    at = 6
    count, at = _read_varint(raw, at)
    inputs = []
    for _ in range(count):
        txid = raw[at:at + 32][::-1].hex()
        vout = int.from_bytes(raw[at + 32:at + 36], "little")
        script_len, after = _read_varint(raw, at + 36)
        assert script_len == 0, "a segwit input has an empty scriptSig"
        inputs.append((txid, vout))
        at = after + 4
    count, at = _read_varint(raw, at)
    outputs = []
    for _ in range(count):
        value = int.from_bytes(raw[at:at + 8], "little")
        script_len, at = _read_varint(raw, at + 8)
        outputs.append((value, raw[at:at + script_len]))
        at += script_len
    witnesses = []
    for _ in inputs:
        items, at = _read_varint(raw, at)
        stack = []
        for _ in range(items):
            size, at = _read_varint(raw, at)
            stack.append(raw[at:at + size])
            at += size
        witnesses.append(stack)
    assert at + 4 == len(raw), "trailing bytes"
    return inputs, outputs, witnesses


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DerivationTests(unittest.TestCase):

    def test_the_bip84_vector_for_the_abandon_mnemonic(self):
        """The first receiving address every BIP-84 wallet shows for these words."""
        key = signer.derive_wallet_key(ABANDON)
        self.assertEqual(key.address, "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")
        self.assertEqual(
            key.public_key.hex(),
            "0330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5f0753bf5beef9c2d91af3c",
        )

    def test_test_networks_use_coin_type_one(self):
        self.assertEqual(
            signer.derive_wallet_key(ABANDON, "", "testnet").address,
            "tb1q6rz28mcfaxtmd6v789l9rrlrusdprr9pqcpvkl",
        )
        self.assertNotEqual(
            signer.derive_wallet_key(ABANDON, "", "regtest").public_key,
            signer.derive_wallet_key(ABANDON, "", "mainnet").public_key,
        )

    def test_bip32_child_derivation_matches_the_published_vector(self):
        # BIP-32 test vector 1: seed 000102...0f, m, then m/0', then m/0'/1.
        import hashlib
        import hmac

        digest = hmac.new(b"Bitcoin seed", bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
                          hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
        self.assertEqual(
            key.hex(), "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35"
        )
        key, chain = signer._child(key, chain, signer.HARDENED)
        self.assertEqual(
            key.hex(), "edb2e14f9ee77d26dd93b4ecede8d16ed408ce149b6cd80b0715a2d911a0afea"
        )
        key, chain = signer._child(key, chain, 1)
        self.assertEqual(
            key.hex(), "3c6cb8d0f6a264c91ea8b5030fadaa8e538b020f0a387421a12de9319dc93368"
        )

    def test_a_passphrase_is_a_different_wallet(self):
        self.assertNotEqual(
            signer.derive_wallet_key(ABANDON, "hunter2").address,
            signer.derive_wallet_key(ABANDON, "").address,
        )

    def test_whitespace_around_the_words_does_not_change_the_wallet(self):
        self.assertEqual(
            signer.derive_wallet_key("  " + ABANDON.replace(" ", "   ") + "\n").address,
            signer.derive_wallet_key(ABANDON).address,
        )

    def test_an_invalid_mnemonic_is_refused_without_repeating_it(self):
        words = "twelve words that are not really twelve words at all no"
        with self.assertRaises(ValueError) as raised:
            signer.derive_wallet_key(words)
        self.assertNotIn("twelve", str(raised.exception))

    def test_an_unknown_network_is_refused(self):
        with self.assertRaises(ValueError):
            signer.derive_wallet_key(ABANDON, "", "litecoin")

    def test_the_private_key_is_not_in_the_repr(self):
        key = signer.derive_wallet_key(ABANDON)
        self.assertNotIn(key.private_key.hex(), repr(key))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GoldenTransactionTests(unittest.TestCase):
    """A fixed transaction, so nothing about serialisation or the sighash drifts quietly."""

    def test_the_transaction_is_byte_for_byte_the_one_verified_externally(self):
        key = signer.derive_wallet_key(ABANDON)
        built = signer.build_transaction(
            key,
            [signer.Utxo("11" * 32, 0, 50_000), signer.Utxo("22" * 32, 1, 120_000)],
            [(script_pubkey_for_address(OTHER), 30_000)],
            fee_rate=5.0,
            op_return=b"deposit-token-1",
        )
        self.assertEqual(built.hex, GOLDEN_HEX)
        self.assertEqual(built.txid, GOLDEN_TXID)
        self.assertEqual(built.fee, 895)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BuildTests(unittest.TestCase):

    def setUp(self):
        self.key = signer.derive_wallet_key(ABANDON)
        self.dest = script_pubkey_for_address(P2WPKH)

    def _build(self, utxos, outputs=None, **kwargs):
        kwargs.setdefault("fee_rate", 5.0)
        return signer.build_transaction(
            self.key, utxos, [(self.dest, 30_000)] if outputs is None else outputs, **kwargs
        )

    @staticmethod
    def _utxo(n, value):
        return signer.Utxo(f"{n:02x}" * 32, 0, value)

    def test_the_fee_is_the_rate_times_the_real_size(self):
        built = self._build([self._utxo(1, 200_000)])
        inputs, outputs, witnesses = parse(built.hex)
        total_out = sum(value for value, _ in outputs)
        self.assertEqual(200_000 - total_out, built.fee)
        # One P2WPKH input, a P2WPKH output and change: 141 vB, which is what the
        # contract reserves for a sweep (`SWEEP_FEE` in test_sweep).
        self.assertEqual(built.fee, 5 * 141)

    def test_outputs_keep_the_positions_asked_for_and_change_comes_last(self):
        built = self._build(
            [self._utxo(1, 200_000)],
            [(self.dest, 30_000), (script_pubkey_for_address(OTHER), 40_000)],
            op_return=b"token",
        )
        _, outputs, _ = parse(built.hex)
        self.assertEqual([value for value, _ in outputs[:3]], [30_000, 40_000, 0])
        self.assertEqual(outputs[0][1], self.dest)
        self.assertEqual(outputs[2][1], b"\x6a\x05token")
        self.assertEqual(outputs[3][1], self.key.script_pubkey)

    def test_the_largest_outputs_are_spent_first_and_only_as_many_as_needed(self):
        built = self._build(
            [self._utxo(1, 5_000), self._utxo(2, 90_000), self._utxo(3, 40_000)]
        )
        inputs, _, _ = parse(built.hex)
        self.assertEqual(inputs, [("02" * 32, 0)])

    def test_several_outputs_are_spent_when_one_is_not_enough(self):
        built = self._build([self._utxo(1, 20_000), self._utxo(2, 20_000)])
        inputs, outputs, witnesses = parse(built.hex)
        self.assertEqual(len(inputs), 2)
        self.assertEqual(len(witnesses), 2)
        # The fee was sized for two inputs, not for the one a smaller payment would use.
        self.assertGreater(built.fee, 5 * 141)

    def test_change_below_dust_goes_to_the_miner_and_is_counted(self):
        # 30_000 + a 705 sat fee leaves 100 sat: not worth an output, and not creatable.
        built = self._build([self._utxo(1, 30_000 + 705 + 100)])
        _, outputs, _ = parse(built.hex)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(built.fee, 705 + 100)

    def test_not_enough_confirmed_funds_is_refused_by_name(self):
        with self.assertRaises(signer.InsufficientFunds):
            self._build([self._utxo(1, 30_000)])
        with self.assertRaises(signer.InsufficientFunds):
            self._build([])

    def test_a_sweep_takes_the_fee_out_of_the_output_and_keeps_the_rest(self):
        built = self._build(
            [self._utxo(1, 1_000_000)], [(self.dest, 600_000)], subtract_fee_from=[0]
        )
        _, outputs, _ = parse(built.hex)
        self.assertEqual(outputs[0][0], 600_000 - built.fee)
        # Change is everything that was not asked to move: the retained hot balance.
        self.assertEqual(outputs[1], (400_000, self.key.script_pubkey))

    def test_a_sweep_that_the_fee_would_reduce_to_dust_is_refused(self):
        with self.assertRaises(signer.InsufficientFunds):
            self._build([self._utxo(1, 1_000_000)], [(self.dest, 800)],
                        subtract_fee_from=[0])

    def test_a_legacy_destination_is_paid(self):
        built = self._build(
            [self._utxo(1, 200_000)], [(script_pubkey_for_address(LEGACY), 50_000)]
        )
        _, outputs, _ = parse(built.hex)
        self.assertEqual(outputs[0][1][:3], b"\x76\xa9\x14")
        self.assertEqual(len(outputs[0][1]), 25)

    def test_the_rate_is_never_below_the_relay_floor(self):
        built = self._build([self._utxo(1, 200_000)], fee_rate=0.2)
        self.assertEqual(built.fee, 141)

    def test_the_witness_is_a_signature_and_this_wallets_key(self):
        built = self._build([self._utxo(1, 200_000)])
        _, _, witnesses = parse(built.hex)
        signature, public_key = witnesses[0]
        self.assertEqual(public_key, self.key.public_key)
        self.assertEqual(signature[-1], signer.SIGHASH_ALL)
        self.assertLessEqual(len(signature), 72)

    def test_signing_is_deterministic(self):
        first = self._build([self._utxo(1, 200_000)])
        second = self._build([self._utxo(1, 200_000)])
        self.assertEqual(first.hex, second.hex)

    def test_an_op_return_over_the_standard_limit_is_refused(self):
        with self.assertRaises(ValueError):
            self._build([self._utxo(1, 200_000)], op_return=b"x" * 81)
        self._build([self._utxo(1, 200_000)], op_return=b"x" * 80)

    def test_nonsense_is_refused_before_anything_is_built(self):
        for outputs in ([], [(self.dest, 0)], [(self.dest, -5)]):
            with self.subTest(outputs=outputs):
                with self.assertRaises(ValueError):
                    self._build([self._utxo(1, 200_000)], outputs)
        with self.assertRaises(ValueError):
            self._build([self._utxo(1, 200_000)], fee_rate=0)
        with self.assertRaises(ValueError):
            self._build([self._utxo(1, 200_000)], subtract_fee_from=[3])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LegacyScriptTests(unittest.TestCase):

    def test_p2pkh_and_p2sh_scripts_for_a_cold_wallet(self):
        p2pkh = script_pubkey_for_address(LEGACY)
        self.assertEqual(p2pkh.hex(), "76a914" + "77bff20c60e522dfaa3350c39b030a5d004e839a" + "88ac")
        p2sh = script_pubkey_for_address("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy")
        self.assertEqual(p2sh[:2], b"\xa9\x14")
        self.assertEqual(p2sh[-1:], b"\x87")

    def test_an_address_for_another_network_has_no_script(self):
        self.assertIsNone(script_pubkey_for_address(LEGACY, network="testnet"))
        self.assertIsNone(script_pubkey_for_address(P2WPKH, network="testnet"))
        self.assertIsNone(script_pubkey_for_address("not an address"))


if __name__ == "__main__":
    unittest.main()
