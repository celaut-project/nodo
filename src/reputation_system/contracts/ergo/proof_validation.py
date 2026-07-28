import json
from typing import List, Optional

from protos import celaut_pb2 as celaut

from src.reputation_system.bip_wallet_verification import bip_ecdsa_sign
from src.reputation_system.contracts.ergo.utils import (
    get_contract_address,
    get_public_key,
    iter_unspent_boxes_by_address,
    owner_proposition_bytes_hex,
)
from src.reputation_system.envs import CONTRACT, ergo_ledger
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import get_script, get_token_id
from src.utils.java_dependency import (
    JavaDependencyMissing,
    ensure_ergpy_jvm,
    require_java_module,
)
from src.utils.logger import LOGGER as logger


def _decode_coll_byte_hex(register_value: str) -> Optional[str]:
    """
    Return the raw byte payload (hex) of a Coll[Byte] register.

    Handles the node's serialized form — ``0e`` (Coll[Byte] type tag) + VLQ length +
    payload — and the explorer's already-rendered raw-hex form. No fixed-length
    assumption: R7 holds the owner ``propositionBytes`` (~35 bytes for a P2PK), R4/R5
    hold 32-byte token ids, etc.
    """
    if not register_value:
        return None

    value = register_value.strip().lower()
    if not value:
        return None

    if value.startswith("0e"):
        # Serialized Coll[Byte]: 0e <VLQ length> <payload bytes>.
        idx = 2
        length = 0
        shift = 0
        try:
            while idx + 1 < len(value):
                byte = int(value[idx:idx + 2], 16)
                idx += 2
                length |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
        except ValueError:
            return None
        payload = value[idx:idx + length * 2]
        return payload or None

    # Already the rendered raw-hex payload.
    return value


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
    if _decode_coll_byte_hex(str(r7) if r7 is not None else "") is None:
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
def validate_reputation_proof_ownership(
        mnemonic_phrase: str = ConfigManager().get("ledgers.ergo.WALLET_MNEMONIC"), 
        proof_id: str = ConfigManager().get("reputation.REPUTATION_PROOF_ID")
    ) -> bool:

    if not proof_id:
        logger('Missing reputation proof id on configuration, run submit reputation.')
        return False

    if not mnemonic_phrase:
        logger("Missing mnemonic while validating reputation proof ownership")
        return False

    try:
        # Obtiene expected_owner (raw propositionBytes) de WALLET_MNEMONIC.
        address = get_public_key(mnemonic_phrase=mnemonic_phrase)
        expected_owner = owner_proposition_bytes_hex(address)

        boxes = _get_unspent_boxes_by_token(proof_id)
        if not boxes:
            logger(f"No boxes found for proof id {proof_id}")
            return False

        # R7 of a Reputation Box holds the owner's raw propositionBytes (Coll[Byte]).
        # The reputation_proof.es contract authorises spends with
        # `INPUTS.exists { b.propositionBytes == SELF.R7[Coll[Byte]].get }`, so R7 is
        # the raw ErgoTree of the owner — NOT blake2b256(propositionBytes).
        box_owners = {
            _decode_coll_byte_hex(str(_extract_register_value(box, "R7") or ""))
            for box in boxes
        }

        # ¡Si alguno de todos los boxes de ese perfil difiere, se considera invalido!
        # Política: el R7 de cada caja del perfil debe ser el propositionBytes de la cartera.
        valid: bool = box_owners == {expected_owner}
        if not valid:
            logger(
                f"Validation failed: expected owner propositionBytes {expected_owner}, "
                f"found R7 values {sorted([h for h in box_owners if h])}"
            )
        return valid
    except Exception as e:
        logger(f"Error validating reputation proof ownership: {e}")
        return False


def __find_reputation_proof_id_for_owner(mnemonic_phrase: str) -> Optional[str]:
    """
    Look up an on-chain reputation proof owned by the given wallet.

    Scans the unspent boxes of the reputation contract (a single address, paginated) and
    returns the proof (token) id of the first box whose R7 equals the wallet's owner
    propositionBytes — breaking as soon as it matches. Returns None when the wallet owns
    no proof.
    """
    node_url = ConfigManager().get("ledgers.ergo.NODE_URL")
    if not node_url:
        raise ValueError("Missing configuration: ledgers.ergo.NODE_URL")

    ensure_ergpy_jvm(feature="Ergo reputation")
    appkit = require_java_module("ergpy.appkit", feature="Ergo reputation")
    ergo = appkit.ErgoAppKit(node_url=node_url)

    owner_proposition = owner_proposition_bytes_hex(get_public_key(mnemonic_phrase=mnemonic_phrase))
    contract_address = get_contract_address(ergo, CONTRACT)

    for box in iter_unspent_boxes_by_address(ergo, contract_address):
        # R7 stores the box owner's raw propositionBytes (Coll[Byte]).
        if _decode_coll_byte_hex(str(_extract_register_value(box, "R7") or "")) != owner_proposition:
            continue

        assets = box.get("assets") or []
        if assets and assets[0].get("tokenId"):
            return assets[0]["tokenId"]

    return None


def sync_reputation_proof_ownership() -> bool:
    """
    Reconcile the locally configured reputation proof with the wallet mnemonic and report
    every step to the user. Wraps the (unmodified) validate_reputation_proof_ownership:

    1. Validate the currently configured proof.
    2. If it is invalid and a proof id is configured, remove it from the config.
    3. If a wallet mnemonic is configured, look up an on-chain reputation proof owned by
       that wallet and, if one exists, store its id in the config.
    4. Print every step for the user.

    Returns True when the node ends up in a coherent state (valid proof, or no proof and
    none discoverable); False when an on-chain lookup failed.
    """
    config = ConfigManager()
    mnemonic_phrase = config.get("ledgers.ergo.WALLET_MNEMONIC") or config.get("WALLET_MNEMONIC")
    proof_id = config.get("reputation.REPUTATION_PROOF_ID") or config.get("REPUTATION_PROOF_ID")

    # 1. Validate current state via the shared function (kept untouched).
    is_valid = validate_reputation_proof_ownership(mnemonic_phrase=mnemonic_phrase, proof_id=proof_id)

    # 2. Drop a configured proof that does not belong to this wallet.
    if is_valid:
        print(f"Reputation proof {proof_id} is valid for the configured wallet.", flush=True)
    else:
        _msg = (
            f"Reputation proof {proof_id} is not owned by the configured wallet; "
            "removing it from the node configuration."
        )
        print(_msg, flush=True)
        logger(_msg)
        config.set("reputation.REPUTATION_PROOF_ID", "")
        proof_id=None

    # 3. With a wallet but no (valid) proof id, try to discover one on-chain.
    if not is_valid and not mnemonic_phrase:  # Can't only be valid in case it has mnemonic and proof_id.
        print(
            "No wallet mnemonic is configured; skipping the on-chain reputation proof lookup.",
            flush=True,
        )
        return False

    if proof_id:
        # Already have a valid, configured proof — nothing to discover.
        return True

    else:
        # In case there is no proof_id
        try:
            discovered_proof_id = __find_reputation_proof_id_for_owner(mnemonic_phrase)
        except JavaDependencyMissing:
            raise
        except Exception as e:
            _msg = f"Could not look up a reputation proof for the configured wallet: {e}"
            print(_msg, flush=True)
            logger(_msg)
            return False

        if discovered_proof_id:
            config.set("reputation.REPUTATION_PROOF_ID", discovered_proof_id)
            _msg = (
                f"Found reputation proof {discovered_proof_id} owned by the configured wallet; "
                "saved it to the node configuration."
            )
            print(_msg, flush=True)
            logger(_msg)
        else:
            print(
                "No reputation proof associated with the configured wallet was found on-chain.",
                flush=True,
            )

        return True
