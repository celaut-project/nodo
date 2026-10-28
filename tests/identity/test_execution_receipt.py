"""Execution receipts (#419): encodings, signatures, chains, and the test vectors.

The vectors at the top are the contract with any other implementation (the chatui
client is in Rust): a fixed key and fixed fields give these exact texts, digests and
signatures. Ed25519 is deterministic (RFC 8032), so the signature is fixed too. If a
vector changes, every receipt already handed out stops verifying elsewhere -- change
one only on purpose, and say so in docs/EXECUTION_RECEIPTS.md.
"""
import hashlib
import unittest

IMPORT_ERROR = None
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from protos import celaut_pb2
    from src.identity import execution_receipt as er
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

# --- the vector inputs -----------------------------------------------------------

VECTOR_PRIVATE_KEY = bytes(range(32))           # 000102...1f
VECTOR_TOKEN = "instance-token-for-vectors"
VECTOR_SERVICE_TYPE = hashlib.sha3_256(b"").digest()
VECTOR_SERVICE_VALUE = hashlib.sha3_256(b"service").digest()
VECTOR_ENVS = {"B": b"\xff\x00", "A": b"1"}
VECTOR_SALT = bytes(range(16))
VECTOR_INITIAL_MU = 12345678
VECTOR_TS = 1798329600
VECTOR_SESSION = "00112233445566778899aabbccddeeff"

# --- the vector outputs ----------------------------------------------------------

VECTOR_PEER_ID = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
VECTOR_EXECUTION_NAME = "b08b762433f9c582f3347fbe192e523260278d24533a94e1972fbcddba42158a"
VECTOR_CANONICAL_ENVS = "client~41~31/client~42~ff00"
VECTOR_ENVS_DIGEST = "e684ff3022c2a14c62addd0129cd4a5d3dc95b2d13a924ce4fc57aa7e9842a45"
VECTOR_START_CANONICAL = (
    "7374617274"                                                          # hex("start")
    "|03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"   # peer_id
    "|b08b762433f9c582f3347fbe192e523260278d24533a94e1972fbcddba42158a"   # execution_name
    "|1798329600"                                                         # ts
    "|a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"   # service_id.type
    "~63e5b5a4137cfa77cb9d10adae081d0df082a826d8441721460d5933f5800056"   # service_id.value
    "|e684ff3022c2a14c62addd0129cd4a5d3dc95b2d13a924ce4fc57aa7e9842a45"   # envs_digest
    "|3132333435363738"                                                   # hex("12345678")
    "||||0|0|0||0|0||||||"                                                # stream fields
    "94c3da627446ed1f3c3736d21ded39451ec00c4ccc687acc42b9d0c61dfcf573"    # scheme digest
)
VECTOR_START_DIGEST = "59ecb3f5e113da7bb9f76d41059a183bfd7d355e9ba32a53d848826be5502531"
VECTOR_START_SIGNATURE = (
    "f79ac345e0d9290dd803edf6b90cf16ef2680ba4353cf44f9a825b38b47be96c"
    "de20b2d09453e24c965a25bbe1b846b7256fbc53fe79f921e88e70d967aa3d0b"
)
VECTOR_IN_HEAD = "4cc36e6c5ce771828a06755610180b0db14686b7b88ec9aca2d94dc67cb82b58"
VECTOR_OUT_HEAD = "721ba2dbda02e1f8d3f9bf151175163f77ece18af91d85a2357a438d36f2ed85"
VECTOR_CHECKPOINT_DIGEST = "94157eae3e1f9aef97bd3347e33e9ed85db5b8c515253881a55e5763f8f2b840"
VECTOR_CHECKPOINT_SIGNATURE = (
    "98bbc219739d19bc7663f99ae68e46d198795cad15d2e9fc5ddd9a8c6b47aef7"
    "a4320777ed11f2d9ffd6fb43c40520e32f14be82193ab64b6cdc6ab74ee6900b"
)
VECTOR_CHECKPOINT_CANONICAL = (
    "636865636b706f696e74"                                                # hex("checkpoint")
    "|03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
    "|b08b762433f9c582f3347fbe192e523260278d24533a94e1972fbcddba42158a"
    "|1798329600"
    "|~||"                                                                # no start fields
    "|74756e6e656c"                                                       # hex("tunnel")
    "|00112233445566778899aabbccddeeff"                                   # session
    "|38303830"                                                           # hex("8080")
    "|1"                                                                  # seq
    "|1|4|4cc36e6c5ce771828a06755610180b0db14686b7b88ec9aca2d94dc67cb82b58"  # in
    "|2|5|721ba2dbda02e1f8d3f9bf151175163f77ece18af91d85a2357a438d36f2ed85"  # out
    "|37"                                                                 # hex("7")
    "|636c69656e742d74756e6e656c"                                         # hex("client-tunnel")
    "||"                                                                  # capture, degraded
    "|94c3da627446ed1f3c3736d21ded39451ec00c4ccc687acc42b9d0c61dfcf573"
)


def _key():
    return ed25519.Ed25519PrivateKey.from_private_bytes(VECTOR_PRIVATE_KEY)


def _signer():
    key = _key()
    return lambda receipt: er.sign_with(receipt, key)


def _start():
    return er.start_receipt(
        VECTOR_TOKEN,
        celaut_pb2.Metadata.HashTag.Hash(type=VECTOR_SERVICE_TYPE, value=VECTOR_SERVICE_VALUE),
        VECTOR_ENVS,
        VECTOR_INITIAL_MU,
        signer=_signer(), peer_id=VECTOR_PEER_ID, ts=VECTOR_TS, salt=VECTOR_SALT,
    )


class _Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _chain(**kwargs):
    defaults = dict(
        signer=_signer(), peer_id=VECTOR_PEER_ID, session=VECTOR_SESSION,
        slot="8080", clock=lambda: VECTOR_TS, every_bytes=1 << 30, every_seconds=1e9,
    )
    defaults.update(kwargs)
    return er.ReceiptChain(VECTOR_TOKEN, er.PLANE_TUNNEL, er.COVERAGE_CLIENT_TUNNEL, **defaults)


def _checkpoint():
    chain = _chain()
    chain.record(er.DIRECTION_IN, b"ping")
    chain.record(er.DIRECTION_OUT, b"pong")
    chain.record(er.DIRECTION_OUT, b"!")
    return chain.checkpoint(mu_charged=7)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VectorTests(unittest.TestCase):
    """Fixed inputs give fixed bytes: what another implementation must reproduce."""

    def test_the_key_of_the_vectors(self):
        receipt = _start()
        self.assertEqual(receipt.peer_id, VECTOR_PEER_ID)

    def test_execution_name_is_blake2b_of_the_token_and_the_raw_key(self):
        expected = hashlib.blake2b(
            VECTOR_TOKEN.encode("utf-8") + bytes.fromhex(VECTOR_PEER_ID), digest_size=32
        ).hexdigest()
        self.assertEqual(er.execution_name(VECTOR_TOKEN, VECTOR_PEER_ID), expected)
        self.assertEqual(expected, VECTOR_EXECUTION_NAME)

    def test_envs(self):
        self.assertEqual(er.canonical_envs(VECTOR_ENVS), VECTOR_CANONICAL_ENVS)
        self.assertEqual(er.envs_digest(VECTOR_ENVS, VECTOR_SALT), VECTOR_ENVS_DIGEST)
        self.assertEqual(
            VECTOR_ENVS_DIGEST,
            hashlib.blake2b(VECTOR_SALT + VECTOR_CANONICAL_ENVS.encode(), digest_size=32).hexdigest(),
        )

    def test_start_receipt(self):
        receipt = _start()
        self.assertEqual(er.canonical_receipt(receipt), VECTOR_START_CANONICAL)
        self.assertEqual(er.receipt_digest(receipt), VECTOR_START_DIGEST)
        self.assertEqual(
            hashlib.blake2b(VECTOR_START_CANONICAL.encode(), digest_size=32).hexdigest(),
            VECTOR_START_DIGEST,
        )
        self.assertEqual(
            er.signed_bytes(receipt),
            f"celaut-execution-receipt:{VECTOR_PEER_ID}|{VECTOR_START_DIGEST}".encode(),
        )
        self.assertEqual(receipt.signature, VECTOR_START_SIGNATURE)
        self.assertTrue(er.verify(receipt, peer_id=VECTOR_PEER_ID, token=VECTOR_TOKEN))

    def test_checkpoint_receipt(self):
        receipt = _checkpoint()
        self.assertEqual(er.canonical_receipt(receipt), VECTOR_CHECKPOINT_CANONICAL)
        self.assertEqual(receipt.in_head, VECTOR_IN_HEAD)
        self.assertEqual(receipt.out_head, VECTOR_OUT_HEAD)
        self.assertEqual(er.receipt_digest(receipt), VECTOR_CHECKPOINT_DIGEST)
        self.assertEqual(receipt.signature, VECTOR_CHECKPOINT_SIGNATURE)
        self.assertTrue(er.verify(receipt, token=VECTOR_TOKEN))

    def test_chain_heads_by_hand(self):
        ping = hashlib.blake2b(b"ping", digest_size=32).hexdigest()
        text = f"{'0' * 64}|1|in|4|{ping}"
        self.assertEqual(hashlib.blake2b(text.encode(), digest_size=32).hexdigest(), VECTOR_IN_HEAD)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EncodingTests(unittest.TestCase):

    def test_the_spelling_of_the_key_does_not_change_the_name(self):
        self.assertEqual(
            er.execution_name(VECTOR_TOKEN, VECTOR_PEER_ID.upper()),
            er.execution_name(VECTOR_TOKEN, VECTOR_PEER_ID),
        )

    def test_a_name_needs_a_real_key(self):
        with self.assertRaises(ValueError):
            er.execution_name(VECTOR_TOKEN, "not-a-key")

    def test_the_digest_reads_raw_bytes_not_decoded_text(self):
        # Both decode to U+FFFD with errors="replace", which is what the envs column of
        # local_instances stores. The digest must tell them apart.
        a = er.envs_digest({"K": b"\xff"}, VECTOR_SALT)
        b = er.envs_digest({"K": b"\xfe"}, VECTOR_SALT)
        self.assertNotEqual(a, b)

    def test_salt_and_origin_change_the_digest(self):
        base = er.envs_digest(VECTOR_ENVS, VECTOR_SALT)
        self.assertNotEqual(base, er.envs_digest(VECTOR_ENVS, bytes(16)))
        self.assertNotEqual(base, er.envs_digest(VECTOR_ENVS, VECTOR_SALT, {"A": er.ORIGIN_NODE}))
        with self.assertRaises(ValueError):
            er.envs_digest(VECTOR_ENVS, VECTOR_SALT, {"A": "elsewhere"})

    def test_the_order_of_the_map_does_not_matter(self):
        self.assertEqual(
            er.envs_digest({"A": b"1", "B": b"2"}, VECTOR_SALT),
            er.envs_digest({"B": b"2", "A": b"1"}, VECTOR_SALT),
        )

    def test_no_envs(self):
        self.assertEqual(er.canonical_envs({}), "")
        self.assertEqual(
            er.envs_digest({}, VECTOR_SALT),
            hashlib.blake2b(VECTOR_SALT, digest_size=32).hexdigest(),
        )

    def test_the_payload_has_its_own_prefix(self):
        from src.identity import node_identity
        payload = node_identity.receipt_payload("p", "d")
        self.assertEqual(payload, "celaut-execution-receipt:p|d")
        for other in (node_identity.statement_payload("p", "d"),
                      node_identity.client_binding_payload("p", "d"),
                      node_identity.attestation_payload("p")):
            self.assertFalse(other.startswith("celaut-execution-receipt:"))

    def test_the_scheme_is_carried_without_prose(self):
        receipt = _start()
        self.assertEqual(len(receipt.signature_scheme.components), 1)
        self.assertEqual(receipt.signature_scheme.components[0].prose, "")
        self.assertTrue(bytes(receipt.signature_scheme.components[0].formal))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TamperTests(unittest.TestCase):
    """Every field but the salt and the signature is covered: changing it breaks the
    signature. Driven by the descriptor, so a new field that the digest forgets fails
    here."""

    UNSIGNED = {"envs_salt", "signature"}

    def _mutate(self, receipt, field):
        descriptor = receipt.DESCRIPTOR.fields_by_name[field]
        if field == "signature_scheme":
            receipt.signature_scheme.components[0].tags.append("other")
        elif field == "envs_salt":
            # Another salt of the same length: a salt of another length is refused.
            salt = bytes(receipt.envs_salt) or bytes(er.ENVS_SALT_BYTES)
            receipt.envs_salt = bytes([salt[0] ^ 1]) + salt[1:]
        elif descriptor.message_type is not None:
            value = getattr(receipt, field)
            if descriptor.message_type.name == "Amount":
                value.n = str(int(value.n or 0) + 1)
            else:
                value.value = bytes(value.value) + b"x"
        elif descriptor.type == descriptor.TYPE_STRING:
            current = getattr(receipt, field)
            if er._is_hex(current, 64):
                setattr(receipt, field, ("1" if current[0] != "1" else "2") + current[1:])
            elif current and set(current) <= er._HEX:
                setattr(receipt, field, ("1" if current[0] != "1" else "2") + current[1:])
            else:
                setattr(receipt, field, current + "x")
        elif descriptor.type == descriptor.TYPE_BYTES:
            setattr(receipt, field, bytes(getattr(receipt, field)) + b"x")
        else:
            setattr(receipt, field, getattr(receipt, field) + 1)

    def _each_field(self, make):
        for field in make().DESCRIPTOR.fields_by_name:
            with self.subTest(field=field):
                receipt = make()
                digest = er.receipt_digest(receipt)
                self._mutate(receipt, field)
                if field in self.UNSIGNED:
                    self.assertEqual(er.receipt_digest(receipt), digest)
                    if field == "envs_salt":
                        # Not signed; and only a start receipt carries a salt.
                        self.assertEqual(er.verify(receipt), receipt.kind == er.KIND_START)
                else:
                    self.assertNotEqual(er.receipt_digest(receipt), digest)
                    self.assertFalse(er.verify(receipt))

    def test_each_field_of_a_start_receipt(self):
        self._each_field(_start)

    def test_each_field_of_a_checkpoint(self):
        self._each_field(_checkpoint)

    def test_a_changed_salt_no_longer_opens_the_envs(self):
        receipt = _start()
        self.assertEqual(er.envs_digest(VECTOR_ENVS, receipt.envs_salt), receipt.envs_digest)
        receipt.envs_salt = bytes(16)
        self.assertNotEqual(er.envs_digest(VECTOR_ENVS, receipt.envs_salt), receipt.envs_digest)

    def test_a_longer_salt_cannot_open_the_envs_with_fewer_variables(self):
        """Final review of #593: nothing separates the salt from the text, so a salt that
        holds the first entry opens the same digest with the rest of the map only."""
        receipt = _start()
        self.assertTrue(er.envs_match(receipt, VECTOR_ENVS))
        first = sorted(VECTOR_ENVS, key=lambda k: k.encode())[0]
        rest = {k: v for k, v in VECTOR_ENVS.items() if k != first}
        long_salt = bytes(receipt.envs_salt) + (
            er.canonical_envs({first: VECTOR_ENVS[first]}) + "/").encode()
        # The digest alone has two readings...
        self.assertEqual(er.envs_digest(rest, long_salt), receipt.envs_digest)
        # ...so the check refuses any salt that is not 16 bytes.
        self.assertFalse(er.envs_match(receipt, rest, salt=long_salt))
        receipt.envs_salt = long_salt
        self.assertFalse(er.envs_match(receipt, rest))
        self.assertIn(f"envs_salt is not {er.ENVS_SALT_BYTES} bytes of a start receipt",
                      er.problems(receipt))

    def test_envs_match_needs_the_right_map_and_a_start(self):
        receipt = _start()
        self.assertFalse(er.envs_match(receipt, {**VECTOR_ENVS, "extra": b"1"}))
        self.assertFalse(er.envs_match(_checkpoint(), VECTOR_ENVS))
        stripped = celaut_pb2.ExecutionReceipt()
        stripped.CopyFrom(receipt)
        stripped.ClearField("envs_salt")          # published without the salt
        self.assertTrue(er.verify(stripped))
        self.assertTrue(er.envs_match(stripped, VECTOR_ENVS, salt=receipt.envs_salt))

    def test_a_separator_cannot_be_moved_between_fields(self):
        receipt = _start()
        receipt.envs_digest = receipt.envs_digest[:10] + "|" + receipt.envs_digest[11:]
        self.assertIn("envs_digest is not a 64-hex digest", er.problems(receipt))

    def test_another_spelling_of_the_key_is_refused(self):
        receipt = _start()
        receipt.peer_id = receipt.peer_id.upper()
        self.assertFalse(er.verify(receipt))

    def test_the_wrong_token_or_node(self):
        receipt = _start()
        self.assertFalse(er.verify(receipt, token="another-token"))
        other = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(32))
        from cryptography.hazmat.primitives import serialization
        other_id = other.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        self.assertFalse(er.verify(receipt, peer_id=other_id))

    def test_a_receipt_signed_by_another_key_under_this_peer_id(self):
        receipt = _start()
        other = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(32))
        receipt.signature = other.sign(er.signed_bytes(receipt)).hex()
        self.assertIn("the signature does not verify", er.problems(receipt))

    def test_a_scheme_this_code_does_not_verify(self):
        receipt = _start()
        receipt.signature_scheme.components[0].formal = b"algorithm=other"
        receipt.signature = _key().sign(er.signed_bytes(receipt)).hex()
        self.assertFalse(er.verify(receipt))


def _resign(receipt):
    """Sign ``receipt`` again with the vector key, as a node that signs a bad shape."""
    receipt.signature = _key().sign(er.signed_bytes(receipt)).hex()
    return receipt


def _observe_checkpoint(coverage=None, capture_mode="pcap"):
    chain = er.ReceiptChain(
        VECTOR_TOKEN, er.PLANE_OBSERVE, coverage or er.COVERAGE_TAP,
        capture_mode=capture_mode, signer=_signer(), peer_id=VECTOR_PEER_ID,
        session=VECTOR_SESSION, clock=lambda: VECTOR_TS)
    chain.record(er.DIRECTION_OUT, b"frame")
    return chain.checkpoint()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ShapeTests(unittest.TestCase):
    """A field that the kind or the plane does not use is empty or zero (the doc). It is
    signed, so a signed receipt with a value there fails ``problems()`` (#593 review)."""

    def _refused(self, receipt, expected):
        found = er.problems(_resign(receipt))
        self.assertTrue(any(expected in p for p in found), found)

    def test_the_valid_shapes(self):
        for receipt in (_start(), _checkpoint(), _observe_checkpoint(),
                        _observe_checkpoint(er.COVERAGE_COUNTERS, "conntrack")):
            with self.subTest(kind=receipt.kind, plane=receipt.plane):
                self.assertEqual(er.problems(receipt), [])

    def test_a_start_receipt_with_a_stream_field(self):
        values = {
            "plane": er.PLANE_TUNNEL, "session": VECTOR_SESSION, "slot": "8080", "seq": 1,
            "in_events": 1, "in_bytes": 1, "in_head": VECTOR_IN_HEAD, "out_events": 1,
            "out_bytes": 1, "out_head": VECTOR_OUT_HEAD, "coverage": er.COVERAGE_TAP,
            "capture_mode": "pcap", "degraded_reason": "x",
        }
        for field, value in values.items():
            with self.subTest(field=field):
                receipt = _start()
                setattr(receipt, field, value)
                self._refused(receipt, f"a start receipt has no stream fields (set: {field})")
        with self.subTest(field="mu_charged"):
            receipt = _start()
            receipt.mu_charged.n = "1"
            self._refused(receipt, "a start receipt has no stream fields (set: mu_charged)")

    def test_a_start_receipt_with_no_service_type(self):
        receipt = _start()
        receipt.service_id.type = b""
        self._refused(receipt, "a start receipt needs service_id")

    def test_a_checkpoint_with_a_start_field(self):
        for field in ("service_id", "envs_digest", "initial_mu"):
            with self.subTest(field=field):
                receipt = _checkpoint()
                if field == "service_id":
                    receipt.service_id.type = VECTOR_SERVICE_TYPE
                elif field == "envs_digest":
                    receipt.envs_digest = VECTOR_ENVS_DIGEST
                else:
                    receipt.initial_mu.n = "1"
                self._refused(receipt, f"a checkpoint has no start fields (set: {field})")

    def test_a_tunnel_checkpoint_with_capture_fields(self):
        for field in ("capture_mode", "degraded_reason"):
            with self.subTest(field=field):
                receipt = _checkpoint()
                setattr(receipt, field, "pcap")
                self._refused(receipt, f"a tunnel checkpoint has no capture fields (set: {field})")

    def test_a_tunnel_checkpoint_needs_slot_and_charge(self):
        for field in ("slot", "mu_charged"):
            with self.subTest(field=field):
                receipt = _checkpoint()
                receipt.ClearField(field)
                self._refused(receipt, "a tunnel checkpoint needs slot and mu_charged")

    def test_an_observe_checkpoint_with_tunnel_fields(self):
        for field in ("slot", "mu_charged"):
            with self.subTest(field=field):
                receipt = _observe_checkpoint()
                if field == "slot":
                    receipt.slot = "8080"
                else:
                    receipt.mu_charged.n = "1"
                self._refused(receipt, f"an observe checkpoint has no tunnel fields (set: {field})")

    def test_a_coverage_of_the_wrong_plane(self):
        receipt = _checkpoint()
        receipt.coverage = er.COVERAGE_TAP
        self._refused(receipt, "is not one of client-tunnel for plane tunnel")
        receipt = _observe_checkpoint()
        receipt.coverage = er.COVERAGE_CLIENT_TUNNEL
        self._refused(receipt, "for plane observe")

    def test_an_instance_tap_needs_pcap(self):
        self._refused(_observe_checkpoint(er.COVERAGE_TAP, "conntrack"),
                      "coverage instance-tap needs capture_mode pcap")
        self._refused(_observe_checkpoint(er.COVERAGE_COUNTERS, ""),
                      "an observe checkpoint needs capture_mode")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChainTests(unittest.TestCase):

    def test_a_client_rebuilds_each_chain_from_its_own_messages(self):
        chain = _chain()
        sent, got = [b"a", b"bb", b"ccc"], [b"x" * 70000, b"y"]
        # The node sees the two directions interleaved in some order; each chain only
        # depends on the order within its direction.
        chain.record(er.DIRECTION_OUT, got[0])
        for message in sent:
            chain.record(er.DIRECTION_IN, message)
        chain.record(er.DIRECTION_OUT, got[1])
        receipt = chain.checkpoint()
        self.assertEqual(er.replay_chain(sent, er.DIRECTION_IN),
                         (receipt.in_events, receipt.in_bytes, receipt.in_head))
        self.assertEqual(er.replay_chain(got, er.DIRECTION_OUT),
                         (receipt.out_events, receipt.out_bytes, receipt.out_head))

    def test_dropping_reordering_or_adding_an_event_changes_the_head(self):
        events = [b"1", b"2", b"3"]
        head = er.replay_chain(events, er.DIRECTION_IN)[2]
        for changed in ([b"1", b"3"], [b"2", b"1", b"3"], [b"1", b"2", b"3", b"4"],
                        [b"1", b"2", b"4"]):
            self.assertNotEqual(er.replay_chain(changed, er.DIRECTION_IN)[2], head)
        # The direction is part of each link.
        self.assertNotEqual(er.replay_chain(events, er.DIRECTION_OUT)[2], head)

    def test_a_length_apart_from_the_payload(self):
        # A counters-only packet: a length and no frame.
        count, total, _ = er.replay_chain([(1500, b"")], er.DIRECTION_IN)
        self.assertEqual((count, total), (1, 1500))

    def test_no_event_is_the_genesis(self):
        receipt = _chain().checkpoint()
        self.assertEqual((receipt.in_head, receipt.out_head),
                         (er.CHAIN_GENESIS, er.CHAIN_GENESIS))

    def test_checkpoints_are_numbered_and_cumulative(self):
        chain = _chain()
        receipts = []
        for n in range(3):
            chain.record(er.DIRECTION_IN, b"m" * (n + 1))
            receipts.append(chain.checkpoint(mu_charged=n))
        receipts.append(chain.checkpoint(mu_charged=9, final=True))
        self.assertEqual([r.seq for r in receipts], [1, 2, 3, 4])
        self.assertEqual([r.in_events for r in receipts], [1, 2, 3, 3])
        self.assertEqual([r.in_bytes for r in receipts], [1, 3, 6, 6])
        self.assertEqual([r.kind for r in receipts], ["checkpoint"] * 3 + ["close"])
        self.assertEqual(len({r.session for r in receipts}), 1)
        self.assertTrue(er.check_sequence(receipts, token=VECTOR_TOKEN)["valid"])

    def test_nothing_after_the_close(self):
        chain = _chain()
        chain.record(er.DIRECTION_IN, b"m")
        self.assertIsNotNone(chain.checkpoint(final=True))
        chain.record(er.DIRECTION_IN, b"late")
        self.assertIsNone(chain.checkpoint())
        self.assertIsNone(chain.checkpoint(final=True))
        self.assertFalse(chain.due())

    def test_due_by_bytes_or_by_time_and_only_after_traffic(self):
        clock = _Clock(100.0)
        chain = _chain(every_bytes=10, every_seconds=5, monotonic=clock)
        self.assertFalse(chain.due())
        clock.now += 60
        self.assertFalse(chain.due())       # idle: no checkpoint
        chain.record(er.DIRECTION_IN, b"123")
        self.assertTrue(chain.due())        # time passed, and there is traffic
        chain.checkpoint()
        chain.record(er.DIRECTION_IN, b"123")
        self.assertFalse(chain.due())
        chain.record(er.DIRECTION_OUT, b"1234567")
        self.assertTrue(chain.due())        # 10 bytes since the last one

    def test_a_signer_that_fails_changes_nothing(self):
        """Sign first, then change the state (#593 review): a failed signature does not
        use the seq and does not close the chain, and it does not raise."""
        clock = _Clock(100.0)
        calls = []

        def flaky(receipt):
            calls.append(receipt.seq)
            if len(calls) <= 2:
                raise RuntimeError("no key")
            er.sign_with(receipt, _key())

        chain = _chain(signer=flaky, every_bytes=1, every_seconds=5, monotonic=clock)
        chain.record(er.DIRECTION_IN, b"ab")
        self.assertTrue(chain.due())
        self.assertIsNone(chain.checkpoint(mu_charged=1))
        self.assertFalse(chain.due())             # waits one every_seconds to try again
        clock.now += 5
        self.assertTrue(chain.due())
        self.assertIsNone(chain.checkpoint(mu_charged=1, final=True))   # close fails too
        chain.record(er.DIRECTION_IN, b"cd")      # the chain is still open
        close = chain.checkpoint(mu_charged=2, final=True)
        self.assertEqual(calls, [1, 1, 1])        # the failed tries did not use the seq
        self.assertEqual((close.kind, close.seq, close.in_events), (er.KIND_CLOSE, 1, 2))
        self.assertTrue(er.verify(close, token=VECTOR_TOKEN))
        self.assertIsNone(chain.checkpoint())

    def test_two_streams_have_two_sessions(self):
        self.assertNotEqual(
            er.ReceiptChain(VECTOR_TOKEN, er.PLANE_TUNNEL, er.COVERAGE_CLIENT_TUNNEL,
                            signer=_signer(), peer_id=VECTOR_PEER_ID).session,
            er.ReceiptChain(VECTOR_TOKEN, er.PLANE_TUNNEL, er.COVERAGE_CLIENT_TUNNEL,
                            signer=_signer(), peer_id=VECTOR_PEER_ID).session,
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SequenceTests(unittest.TestCase):

    def _stream(self, n=3, close=True):
        chain = _chain(session=None)
        out = []
        for i in range(n):
            chain.record(er.DIRECTION_IN, b"m")
            out.append(chain.checkpoint(mu_charged=i))
        if close:
            out.append(chain.checkpoint(mu_charged=n, final=True))
        return out

    def test_a_valid_set(self):
        report = er.check_sequence([_start()] + self._stream(), token=VECTOR_TOKEN)
        self.assertTrue(report["valid"], report)
        stream = next(iter(report["streams"].values()))
        self.assertTrue(stream["closed"])
        self.assertEqual(stream["last_seq"], 4)

    def test_two_signed_receipts_for_one_seq_are_a_contradiction(self):
        stream = self._stream(close=False)
        forged = celaut_pb2.ExecutionReceipt()
        forged.CopyFrom(stream[1])
        forged.in_bytes += 100
        forged.in_events += 1
        _signer()(forged)  # the same key signs both: the node lied
        report = er.check_sequence(stream + [forged])
        self.assertEqual(len(report["contradictions"]), 1)
        self.assertFalse(report["valid"])

    def test_two_start_receipts_for_one_name(self):
        a, b = _start(), _start()
        b.initial_mu.n = "1"
        _signer()(b)
        self.assertEqual(len(er.check_sequence([a, b])["contradictions"]), 1)

    def test_a_counter_that_goes_down(self):
        stream = self._stream(close=False)
        stream[2].in_bytes = 0
        _signer()(stream[2])
        report = er.check_sequence(stream)
        self.assertTrue(any("goes down" in e for e in report["errors"]))

    def test_a_checkpoint_after_the_close(self):
        stream = self._stream()
        late = celaut_pb2.ExecutionReceipt()
        late.CopyFrom(stream[-1])
        late.kind, late.seq = er.KIND_CHECKPOINT, late.seq + 1
        _signer()(late)
        report = er.check_sequence(stream + [late])
        self.assertTrue(any("after its close" in e for e in report["errors"]))

    def test_a_gap_is_reported_but_is_not_an_error(self):
        stream = self._stream()
        report = er.check_sequence([stream[0], stream[2], stream[3]])
        self.assertEqual(report["gaps"], [{"session": stream[0].session, "from": 2, "to": 2}])
        self.assertTrue(report["valid"])

    def test_a_tampered_receipt_in_the_set(self):
        stream = self._stream()
        stream[1].out_bytes += 1
        report = er.check_sequence(stream)
        self.assertFalse(report["valid"])
        self.assertFalse(report["receipts"][1]["valid"])

    def test_an_empty_set_is_not_valid(self):
        self.assertFalse(er.check_sequence([])["valid"])

    def test_json_round_trip(self):
        receipt = _start()
        self.assertEqual(er.from_json_dict(er.to_json_dict(receipt)), receipt)
        self.assertIn("envs_salt", er.to_json_dict(receipt))
        self.assertIn("execution_name", er.to_json_dict(receipt))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CacheTests(unittest.TestCase):
    """The open path of a tunnel or an Observe session reads the config and the key
    once per process, not once per stream (#593 review)."""

    def setUp(self):
        self._saved = (er._intervals, er._node_key)
        er._intervals = er._node_key = None

    def tearDown(self):
        er._intervals, er._node_key = self._saved

    def test_the_intervals_are_read_once(self):
        from unittest import mock
        from src.utils import config as config_module
        with mock.patch.object(config_module.ConfigManager, "get",
                               side_effect=lambda key, default=None: 7) as get:
            self.assertEqual(er.checkpoint_intervals(), (7, 7.0))
            self.assertEqual(er.checkpoint_intervals(), (7, 7.0))
        self.assertEqual(get.call_count, 2)       # two keys, read one time

    def test_the_key_is_read_once(self):
        from unittest import mock
        from src.identity import node_identity
        mnemonic = "abandon " * 11 + "about"
        with mock.patch.object(node_identity, "get_identity_mnemonic",
                               return_value=mnemonic) as read:
            peer_id = er.node_peer_id()
            for _ in range(3):
                er.ReceiptChain.for_node(VECTOR_TOKEN, er.PLANE_TUNNEL,
                                         er.COVERAGE_CLIENT_TUNNEL, slot="1")
            self.assertEqual(er.node_peer_id(), peer_id)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(peer_id, node_identity._cached_keypair(mnemonic)[0])

    def test_no_key_is_not_kept(self):
        from unittest import mock
        from src.identity import node_identity
        with mock.patch.object(node_identity, "get_identity_mnemonic", return_value=None):
            self.assertIsNone(er.node_signer())
        with mock.patch.object(node_identity, "get_identity_mnemonic",
                               return_value="abandon " * 11 + "about"):
            self.assertIsNotNone(er.node_signer())

    def test_the_execution_name_of_this_node(self):
        from unittest import mock
        with mock.patch.object(er, "node_peer_id", return_value=VECTOR_PEER_ID):
            self.assertEqual(er.node_execution_name(VECTOR_TOKEN), VECTOR_EXECUTION_NAME)
        with mock.patch.object(er, "node_peer_id", return_value=None):
            self.assertIsNone(er.node_execution_name(VECTOR_TOKEN))


if __name__ == "__main__":
    unittest.main()
