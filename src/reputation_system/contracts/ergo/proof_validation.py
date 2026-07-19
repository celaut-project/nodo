import hashlib
import json
import re
from typing import List, Optional

from protos import celaut_pb2 as celaut

from src.reputation_system.bip_wallet_verification import bip_ecdsa_sign
from src.reputation_system.contracts.ergo.utils import get_public_key
from src.reputation_system.envs import CONTRACT, ergo_ledger
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import get_script, get_token_id
from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module
from src.utils.logger import LOGGER as logger


_HEX_64_RE = re.compile(r"[0-9a-fA-F]{64}")


def _extract_r7_hash_hex(register_value: str) -> Optional[str]:
    if not register_value:
        return None

    value = register_value.strip().lower()
    # Serialized Coll[Byte] of 32 bytes normally starts with 0e20
    if value.startswith("0e20") and len(value) >= 68:
        candidate = value[4:68]
        if _HEX_64_RE.fullmatch(candidate):
            return candidate

    # Fallback: find any 64-hex chunk.
    match = _HEX_64_RE.search(value)
    return match.group(0).lower() if match else None


def _extract_register_value(box: dict, register: str) -> Optional[str]:
    additional = box.get("additionalRegisters", {})
    reg = additional.get(register)

    if reg is None:
        return None

    if isinstance(reg, str):
        return reg

    if isinstance(reg, dict):
        return reg.get("serializedValue") or reg.get("renderedValue")

    return None


def _get_unspent_boxes_by_token(token_id: str) -> List[dict]:
    from src.reputation_system.contracts.ergo.utils import get_boxes_by_token_ids

    node_url = ConfigManager().get("ledgers.ergo.NODE_URL")
    if not node_url:
        raise ValueError("Missing configuration: ledgers.ergo.NODE_URL")

    ensure_ergpy_jvm(feature="Ergo reputation")
    appkit = require_java_module("ergpy.appkit", feature="Ergo reputation")
    ergo = appkit.ErgoAppKit(node_url=node_url)

    java_boxes = get_boxes_by_token_ids(ergo, node_url, [token_id])
    return [json.loads(str(box.toJson(True))) for box in java_boxes]


def _validate_box_structure(box: dict) -> bool:
    additional = box.get("additionalRegisters")
    if not isinstance(additional, dict):
        return False

    required = {"R4", "R5", "R6", "R7", "R8", "R9"}
    if not required.issubset(additional.keys()):
        return False

    r6 = _extract_register_value(box, "R6")
    r8 = _extract_register_value(box, "R8")
    r7 = _extract_register_value(box, "R7")

    if r6 is None or str(r6).lower() not in {"true", "false", "0400", "0401"}:
        return False
    if r8 is None or str(r8).lower() not in {"true", "false", "0400", "0401"}:
        return False
    if _extract_r7_hash_hex(str(r7) if r7 is not None else "") is None:
        return False

    assets = box.get("assets", [])
    if not isinstance(assets, list) or len(assets) == 0:
        return False

    return True

"""
    Valida si el Perfil de Reputación es soportado por el nodo y si existe en la red. 
    Utilizado para validar Perfil de un par antes de almacenarlo.
"""
def validate_contract_ledger(contract_ledger: celaut.Contract, peer_id: str) -> bool:
    _ = peer_id  # retained to keep public signature stable.

    compatibility = (
        contract_ledger.ledger.formal == ergo_ledger.formal
        and get_script(contract_ledger) == CONTRACT.encode("utf-8")
    )  # TODO Could check at Reputation System to consider tag-prose-formal equivalences.

    if not compatibility:
        logger(
            "Contract ledger not compatible: "
            f"ledger={contract_ledger.ledger.formal == ergo_ledger.formal} "
            f"script={get_script(contract_ledger) == CONTRACT.encode('utf-8')}"
        )
        return False

    token_id = get_token_id(contract_ledger)
    if not token_id:
        logger("Incomplete contract ledger, there is no token id")
        return False

    try:
        boxes = _get_unspent_boxes_by_token(token_id)
    except Exception as e:
        logger(f"Error fetching token boxes for structural validation: {e}")
        return False

    if not boxes:
        logger(f"No unspent boxes found for proof token {token_id}")
        return False

    if not all(_validate_box_structure(box) for box in boxes):
        logger("Structural validation failed for one or more reputation boxes")
        return False

    return True


"""
    Firma un mensaje con WALLET_MNEMONIC para demostrarle a un tercero autenticidad.
"""
def sign_message(public_key, message) -> str | None:
    mnemonic_phrase = ConfigManager().get("ledgers.ergo.WALLET_MNEMONIC") or ConfigManager().get("WALLET_MNEMONIC")
    if not mnemonic_phrase:
        logger("Missing wallet mnemonic configuration")
        return None
    
    address = get_public_key(mnemonic_phrase=mnemonic_phrase)
    if address.toString() is not public_key:
        logger(f"Public_key {public_key} not mine.")

    # Keep API compatibility: sign request if caller-provided public key is non-empty.
    if not public_key:
        logger("Public key is required")
        return None

    signed_msg = bip_ecdsa_sign(mnemonic_phrase=mnemonic_phrase, message=message)
    logger(f"Message signed successfully for public key: {public_key}")
    return signed_msg


"""
    Verifica que el Perfil de Reputación y la cartera almacenados en la configuración están relacionados (el perfil pertenece a esa cartera).
"""
def validate_reputation_proof_ownership() -> bool:
    mnemonic_phrase = ConfigManager().get("ledgers.ergo.WALLET_MNEMONIC") or ConfigManager().get("WALLET_MNEMONIC")
    proof_id = ConfigManager().get("reputation.REPUTATION_PROOF_ID") or ConfigManager().get("REPUTATION_PROOF_ID")

    if not proof_id:
        logger('Missing reputation proof id on configuration, run submit reputation.')
        return False

    if not mnemonic_phrase:
        logger("Missing mnemonic while validating reputation proof ownership")
        return False

    try:
        # Obtiene expected_owner_hash de WALLET_MNEMONIC
        address = get_public_key(mnemonic_phrase=mnemonic_phrase)
        ergo_tree = address.getErgoAddress().script()
        jpype = require_java_module("jpype", feature="Ergo reputation")
        serializer = jpype.JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
        proposition_bytes = bytes((byte + 256) % 256 for byte in serializer.serializeErgoTree(ergo_tree))
        expected_owner_hash = hashlib.blake2b(proposition_bytes, digest_size=32).hexdigest()

        boxes = _get_unspent_boxes_by_token(proof_id)
        if not boxes:
            logger(f"No boxes found for proof id {proof_id}")
            return False

        # Context: R7 of a Reputation Box is R7 	Coll[Byte] 	blake2b256(propositionBytes) of the owner script. 
        # (https://github.com/reputation-systems/reputation-system#22-reputation-token-contract-reputation-box)
        box_hashes = {
            _extract_r7_hash_hex(str(_extract_register_value(box, "R7") or ""))
            for box in boxes
        }

        # Comprobamos si el blake2b256(propositionBytes) de WALLET_MNEMONIC es igual al almacenado en R7 del reputation profile
        # 
        # ¡Si alguno de todos los boxes de ese perfil difiere, se considera invalido! 
        # Esta política podría cambiar:
        # 1. Si expected owner hash se encuentra en cualquier R7, se considera valido.
        # 2. Únicamente se mira la caja donde R5 === profile token id.
        # 
        valid: bool = box_hashes == {expected_owner_hash}
        if not valid:
            logger(
                f"Validation failed: expected owner hash {expected_owner_hash}, "
                f"found R7 hashes {sorted([h for h in box_hashes if h])}"
            )
        return valid
    except Exception as e:
        logger(f"Error validating reputation proof ownership: {e}")
        return False
