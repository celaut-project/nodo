from src.reputation_system import envs
from src.reputation_system.contracts.ergo import proof_validation


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


def test_extract_r7_hash_hex_from_serialized_coll_byte():
    expected = "a" * 64
    serialized = f"0e20{expected}"
    assert proof_validation._extract_r7_hash_hex(serialized) == expected


def test_validate_box_structure_accepts_required_registers():
    box = {
        "additionalRegisters": {
            "R4": "0e20" + "01" * 32,
            "R5": "0e20" + "02" * 32,
            "R6": "true",
            "R7": "0e20" + "03" * 32,
            "R8": "false",
            "R9": "0e20" + "04" * 32,
        },
        "assets": [{"tokenId": "tok", "amount": 1}],
    }
    assert proof_validation._validate_box_structure(box)


def test_validate_box_structure_rejects_legacy_or_missing_r7_hash():
    box = {
        "additionalRegisters": {
            "R4": "0e20" + "01" * 32,
            "R5": "0e20" + "02" * 32,
            "R6": "true",
            "R7": "0008cd",  # not a 32-byte hash payload
            "R8": "false",
            "R9": "0e20" + "04" * 32,
        },
        "assets": [{"tokenId": "tok", "amount": 1}],
    }
    assert not proof_validation._validate_box_structure(box)
