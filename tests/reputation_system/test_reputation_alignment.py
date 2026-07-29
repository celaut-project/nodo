from src.reputation_system import envs
from src.reputation_system.contracts.ergo import proof_validation, transaction


# Canonical reputation-proof register spec — enforced by reputation_proof.es and shared
# by every ecosystem reader (reputation-systems web app, Game of Prompts, skills, forum):
#   R4 Coll[Byte] = typeNftTokenId          (raw token-id bytes)
#   R5 Coll[Byte] = uniqueObjectData         (raw bytes; a self-profile points to its own token id)
#   R7 Coll[Byte] = owner propositionBytes   (raw ErgoTree — NOT blake2b256(propositionBytes))
# Ids are stored as their RAW bytes, never as UTF-8 text of the hex string.

TYPE_NFT = "64060577c3393e0e3cf8938ec8e6a2002ded27ece17750aa5add7d5c3e1227ba"
PROOF_ID = "d4e7c77ca41e7a950cb6c46fcc5da4a91ae4021aceb718ba651f74b750ff4b2a"
P2PK_PROPOSITION = "0008cd" + "02" * 33  # realistic owner ErgoTree (36 bytes)


def _coll_byte(data: bytes) -> str:
    """Serialize bytes as an Ergo Coll[Byte]: 0e + VLQ(len) + payload."""
    n = len(data)
    vlq = b""
    while True:
        b = n & 0x7F
        n >>= 7
        vlq += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            break
    return "0e" + vlq.hex() + data.hex()


def test_reputation_contract_is_the_canonical_ecosystem_instance():
    # nodo must mint/scan proofs on the exact ErgoTree-v1 contract that
    # reputation-systems/reputation-system publishes. A different ErgoTree version,
    # or a Digital-Public-Good hash derived from the .es source text instead of the
    # compiled ErgoTree bytes, lands the box at a P2S address that system cannot read.
    # These values are pinned and were verified against the on-chain contract holding
    # every live proof (see the derivation notes in envs.py).
    assert (
        envs.DIGITAL_PUBLIC_GOOD_SCRIPT_HASH
        == "ceea52651b6b206381ea28a2e59f775367cef567c0c2f089dc7e09356b64ef61"
    )
    assert len(envs.DIGITAL_PUBLIC_GOOD_SCRIPT_HASH) == 64
    int(envs.DIGITAL_PUBLIC_GOOD_SCRIPT_HASH, 16)  # valid hex

    assert envs.REPUTATION_PROOF_ADDRESS.startswith("6axptaZbz6n5h3MUjsWMf4pt")
    assert len(envs.REPUTATION_PROOF_ADDRESS) == 1204

    # The pinned ErgoTree is valid hex and an ErgoTree-v1 tree (header byte 0x19).
    tree = envs.REPUTATION_PROOF_ERGO_TREE
    assert tree.startswith("19")
    assert bytes.fromhex(tree)  # no odd length / non-hex


def test_id_bytes_encodes_hex_ids_as_raw_bytes():
    # Token ids / hashes -> raw bytes (NOT UTF-8 of the hex string).
    assert transaction._id_bytes(TYPE_NFT) == bytes.fromhex(TYPE_NFT)
    assert len(transaction._id_bytes(TYPE_NFT)) == 32
    # Genuine free-text pointers fall back to UTF-8 (mirrors hexOrUtf8ToBytes).
    assert transaction._id_bytes("No IP available.") == b"No IP available."
    assert transaction._id_bytes("") == b""


def test_looks_like_hex():
    assert transaction._looks_like_hex(TYPE_NFT)
    assert not transaction._looks_like_hex("No IP available.")
    assert not transaction._looks_like_hex("abc")  # odd length


def test_decode_coll_byte_hex_roundtrips_ids_and_proposition():
    # R4/R5 token ids round-trip: raw-byte encode -> serialized Coll[Byte] -> decode.
    for value in (TYPE_NFT, PROOF_ID):
        assert proof_validation._decode_coll_byte_hex(_coll_byte(transaction._id_bytes(value))) == value
    # R7 raw propositionBytes (variable length) round-trips.
    assert (
        proof_validation._decode_coll_byte_hex(_coll_byte(bytes.fromhex(P2PK_PROPOSITION)))
        == P2PK_PROPOSITION
    )


def test_decode_coll_byte_hex_handles_multibyte_vlq_and_rendered_form():
    big = bytes(range(200))  # payload > 127 bytes -> multi-byte VLQ length
    assert proof_validation._decode_coll_byte_hex(_coll_byte(big)) == big.hex()
    # Explorer's already-rendered raw-hex form (no 0e tag) passes through.
    assert proof_validation._decode_coll_byte_hex(TYPE_NFT) == TYPE_NFT


def test_decode_rejects_the_old_utf8_double_encoding():
    # Regression guard for the bug this fixes: old nodo stored R4 as UTF-8 of the hex
    # STRING, so a raw-byte reader (the whole ecosystem) decodes it to the hex-of-ASCII,
    # never the real token id -> the proof is invisible in the web app.
    legacy_utf8 = _coll_byte(TYPE_NFT.encode("utf-8"))
    assert proof_validation._decode_coll_byte_hex(legacy_utf8) != TYPE_NFT


def test_validate_box_structure_accepts_canonical_registers():
    box = {
        "additionalRegisters": {
            "R4": _coll_byte(bytes.fromhex(TYPE_NFT)),
            "R5": _coll_byte(bytes.fromhex(PROOF_ID)),
            "R6": "true",
            "R7": _coll_byte(bytes.fromhex(P2PK_PROPOSITION)),
            "R8": "false",
            "R9": "0e20" + "04" * 32,
        },
        "assets": [{"tokenId": PROOF_ID, "amount": 1}],
    }
    assert proof_validation._validate_box_structure(box)


def test_validate_box_structure_rejects_missing_r7():
    box = {
        "additionalRegisters": {
            "R4": _coll_byte(bytes.fromhex(TYPE_NFT)),
            "R5": _coll_byte(bytes.fromhex(PROOF_ID)),
            "R6": "true",
            "R7": "",  # empty -> undecodable owner
            "R8": "false",
            "R9": "0e20" + "04" * 32,
        },
        "assets": [{"tokenId": PROOF_ID, "amount": 1}],
    }
    assert not proof_validation._validate_box_structure(box)


def test_off_canonical_contract_rejects_v0_and_accepts_v1():
    # A proof box on the canonical ErgoTree-v1 contract is NOT flagged...
    canonical = envs.REPUTATION_PROOF_ERGO_TREE
    assert proof_validation._boxes_off_canonical_contract([{"ergoTree": canonical}]) == []
    # ...but a box on a different (e.g. locally-recompiled ErgoTree-v0) contract IS flagged,
    # so validate_reputation_proof_ownership rejects the wallet-owned-but-invisible proof.
    v0_tree = "101c0400040004000400"  # header 0x10 => ErgoTree v0
    assert proof_validation._boxes_off_canonical_contract([{"ergoTree": v0_tree}]) == [v0_tree]
    # A box without an ergoTree is left to the ownership check, not flagged here.
    assert proof_validation._boxes_off_canonical_contract([{}]) == []
    # Case-insensitive match.
    assert proof_validation._boxes_off_canonical_contract([{"ergoTree": canonical.upper()}]) == []


def test_find_reputation_proof_id_uses_defined_owner_helper():
    # Regression: the discovery function once referenced an undefined `owner_script_hash_hex`
    # (dropped in a rename), crashing sync with NameError. It must reference the helper that
    # actually exists in utils, `owner_proposition_bytes_hex`, matching validate's owner check.
    fn = proof_validation.__dict__["__find_reputation_proof_id_for_owner"]
    names = fn.__code__.co_names
    assert "owner_script_hash_hex" not in names
    assert "owner_proposition_bytes_hex" in names
