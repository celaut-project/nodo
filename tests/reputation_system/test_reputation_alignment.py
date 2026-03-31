from src.reputation_system import envs
from src.reputation_system.contracts.ergo import proof_validation


def test_envs_contract_placeholder_is_resolved():
    assert "`+DIGITAL_PUBLIC_GOOD_SCRIPT_HASH+`" not in envs.CONTRACT
    assert len(envs.DIGITAL_PUBLIC_GOOD_SCRIPT_HASH) == 64
    int(envs.DIGITAL_PUBLIC_GOOD_SCRIPT_HASH, 16)


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
