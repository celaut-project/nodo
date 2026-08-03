import os
from typing import List, Optional

import grpc
from bee_rpc import client as bee

from protos import celaut_pb2 as celaut
from protos import celaut_pb2_grpc

from src.reputation_system.bip_wallet_verification import (
    bip_ecdsa_sign,
    bip_ecdsa_verify_proposition,
)
from src.reputation_system.contracts.ergo.utils import (
    get_public_key,
    iter_unspent_boxes_by_address,
    owner_proposition_bytes,
    owner_proposition_bytes_hex,
)
from src.reputation_system.envs import (
    REPUTATION_PROOF_ADDRESS,
    REPUTATION_PROOF_ERGO_TREE,
    ergo_ledger,
)
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import get_script, get_token_id
from src.utils.java_dependency import (
    JavaDependencyMissing,
    ensure_ergpy_jvm,
    require_java_module,
)
from src.utils.logger import LOGGER as logger

# Ownership-challenge parameters.
_CHALLENGE_TIMEOUT_SECONDS = 20
_CHALLENGE_NONCE_BYTES = 32


class ProofLookupUnavailable(ValueError):
    """The Ergo node could not be queried, so a proof's status is UNDETERMINED.

    Distinct from a negative verdict ("the chain says this proof is not yours").
    Callers that act destructively on a negative — dropping REPUTATION_PROOF_ID
    from the config, or minting a fresh proof instead of spending the existing
    one — must not treat an unreachable node as one. Subclasses ValueError so
    existing callers that only expect that keep working.
    """


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


def _boxes_off_canonical_contract(boxes: List[dict]) -> List[str]:
    """
    Return the ErgoTrees of any boxes NOT sitting on the canonical reputation contract.

    A proof the wallet "owns" (its R7 matches) but whose box lives on a different contract
    instance — e.g. a locally-recompiled ErgoTree v0 — is invisible to
    reputation-systems/reputation-system. Boxes without an ``ergoTree`` field are left to
    the ownership check (not flagged here).
    """
    canonical = REPUTATION_PROOF_ERGO_TREE.lower()
    return [
        b["ergoTree"]
        for b in boxes
        if b.get("ergoTree") and b["ergoTree"].lower() != canonical
    ]


def _node_own_proof_token_id(box: dict, owner_proposition: str, node_type_nft: str) -> Optional[str]:
    """
    If ``box`` is the node's OWN reputation proof owned by ``owner_proposition``, return its
    token id; otherwise None.

    nodo mints its proof with R7 = owner propositionBytes, R4 = the CELAUT node-type NFT,
    and R5 self-pointing to the proof's own token id (see transaction.py). The wallet may
    also own unrelated proofs — e.g. a user profile of a different type, or reputation-edge
    boxes pointing at another object — which must NOT be adopted as the node's proof.
    """
    if _decode_coll_byte_hex(str(_extract_register_value(box, "R7") or "")) != owner_proposition:
        return None

    assets = box.get("assets") or []
    token_id = assets[0].get("tokenId") if assets else None
    if not token_id:
        return None

    r4 = (_decode_coll_byte_hex(str(_extract_register_value(box, "R4") or "")) or "").lower()
    r5 = (_decode_coll_byte_hex(str(_extract_register_value(box, "R5") or "")) or "").lower()

    # R5 must self-point: an identity/node proof, not a reputation edge to another object.
    if r5 != token_id.lower():
        return None
    # When configured, R4 must be the node-type NFT — never a user PROFILE_TYPE_NFT etc.
    if node_type_nft and r4 != node_type_nft.lower():
        return None

    return token_id


def _get_unspent_boxes_by_token(token_id: str) -> List[dict]:
    """
    Unspent boxes holding ``token_id``, as the Ergo node's own JSON.

    Read straight from ``GET /blockchain/box/byTokenId/{token_id}``: that response
    already carries every box in full, with ``ergoTree`` as canonical hex and
    ``additionalRegisters`` in the serialized ``0e…`` form that
    :func:`_boxes_off_canonical_contract`, :func:`_validate_box_structure` and
    :func:`_decode_coll_byte_hex` all expect — the same shape
    :func:`iter_unspent_boxes_by_address` yields on the ownership-lookup path.

    Do NOT route this through AppKit's ``InputBox.toJson()``: it renders
    ``ergoTree`` as the Scala object's ``toString``
    (``ErgoTree(25,ArraySeq(IntConstant(0),…``) instead of hex, so every hex
    comparison against the canonical contract failed and *every* peer proof was
    rejected as "off the canonical contract". It also cost a JVM start plus
    minutes of wall clock per validation.

    The endpoint returns the token's whole history, so spent boxes are dropped
    here: one stale box would be enough to fail an otherwise valid proof.

    Raises :class:`ProofLookupUnavailable` when the node cannot be queried, so
    callers can tell "the chain says no" from "the chain did not answer".
    """
    import requests

    node_url = ConfigManager().get("ledgers.ergo.NODE_URL")
    if not node_url:
        raise ProofLookupUnavailable("Missing configuration: ledgers.ergo.NODE_URL")

    url = f"{str(node_url).rstrip('/')}/blockchain/box/byTokenId/{token_id}"
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as e:
        raise ProofLookupUnavailable(f"Could not reach the Ergo node at {node_url}: {e}")

    if response.status_code != 200:
        raise ProofLookupUnavailable(
            f"Could not fetch token {token_id}: HTTP {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as e:
        raise ProofLookupUnavailable(f"Unreadable response for token {token_id}: {e}")

    items = payload.get("items") if isinstance(payload, dict) else None
    return [box for box in (items or []) if not box.get("spentTransactionId")]


def _validate_box_structure(box: dict) -> bool:
    """
    Structural validation of a single reputation box, replacing the former ``return True``.

    Enforces the canonical register layout shared by the whole ecosystem
    (reputation_proof.es):
        R4 Coll[Byte] = type NFT token id    (present, decodable)
        R5 Coll[Byte] = unique object data   (present, decodable)
        R7 Coll[Byte] = owner propositionBytes (present, decodable, non-empty)
    plus a reputation token in ``assets`` and — when the box carries an ``ergoTree`` — a
    match against the canonical reputation contract ErgoTree. Boxes without an ``ergoTree``
    field are deferred to :func:`_boxes_off_canonical_contract` / the ownership check.
    """
    for register in ("R4", "R5", "R7"):
        decoded = _decode_coll_byte_hex(str(_extract_register_value(box, register) or ""))
        if not decoded:
            logger(f"Reputation box structure invalid: register {register} missing or undecodable.")
            return False

    assets = box.get("assets") or []
    token_id = assets[0].get("tokenId") if assets else None
    if not token_id:
        logger("Reputation box structure invalid: no reputation token in assets.")
        return False

    ergo_tree = box.get("ergoTree")
    if ergo_tree and ergo_tree.lower() != REPUTATION_PROOF_ERGO_TREE.lower():
        logger("Reputation box structure invalid: ergoTree does not match the canonical contract.")
        return False

    return True


def _challenge_peer_ownership(peer_id: str, owner_proposition_hex: str) -> bool:
    """
    Cryptographically prove that ``peer_id`` controls the R7 owner ``owner_proposition_hex``.

    Creates a fresh random challenge, calls the peer's ``Gateway.SignPublicKey`` over gRPC
    with the raw ``proposition_bytes`` + challenge, and verifies the returned signature
    against the public key embedded in those proposition bytes. Any RPC error, malformed
    response, timeout/expiry, or verification failure returns ``False`` (never raises).
    """
    from src.utils.utils import generate_uris_by_peer_id

    try:
        proposition_bytes = bytes.fromhex(owner_proposition_hex)
    except (ValueError, TypeError):
        logger(f"Ownership challenge: R7 owner {owner_proposition_hex!r} is not valid hex.")
        return False

    uri = next(generate_uris_by_peer_id(peer_id=peer_id), None)
    if uri is None:
        logger(f"Ownership challenge: no reachable URI for peer {peer_id}.")
        return False

    challenge = os.urandom(_CHALLENGE_NONCE_BYTES).hex()
    try:
        stub = celaut_pb2_grpc.GatewayStub(grpc.insecure_channel(uri))
        response = next(
            bee.client_grpc(
                method=stub.SignPublicKey,
                partitions_message_mode_parser=True,
                input=celaut.SignRequest(
                    public_key=proposition_bytes.hex(),
                    to_sign=challenge,
                ),
                indices_parser=celaut.SignResponse,
                timeout=_CHALLENGE_TIMEOUT_SECONDS,
            ),
            None,
        )
    except grpc.RpcError as e:
        logger(f"Ownership challenge RPC to peer {peer_id} failed: {e}")
        return False
    except Exception as e:  # bee parse / transport errors
        logger(f"Ownership challenge to peer {peer_id} errored: {e}")
        return False

    if response is None or not response.signed:
        logger(f"Ownership challenge: peer {peer_id} returned no signature.")
        return False

    signature_hex = response.signed or ""
    if not bip_ecdsa_verify_proposition(proposition_bytes, challenge, signature_hex):
        logger(f"Ownership challenge: peer {peer_id} signature did not verify against R7.")
        return False

    return True


"""
    Valida si el Perfil de Reputación es soportado por el nodo y si existe en la red.
    Utilizado para validar Perfil de un par antes de almacenarlo.
"""
def validate_contract_ledger(contract_ledger: celaut.Contract, peer_id: str) -> bool:
    # Equivalence policy: `formal` is the canonical machine-readable ledger identity, so we
    # validate ONLY the compiled ErgoTree (get_script) plus `formal`. `tags`/`prose` are
    # human-facing and intentionally not part of the compatibility decision.
    expected_script = bytes.fromhex(REPUTATION_PROOF_ERGO_TREE)
    compatibility = (
        contract_ledger.ledger.formal == ergo_ledger.formal
        and get_script(contract_ledger) == expected_script
    )

    if not compatibility:
        logger(
            "Contract ledger not compatible: "
            f"ledger={contract_ledger.ledger.formal == ergo_ledger.formal} "
            f"script={get_script(contract_ledger) == expected_script}"
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

    # Reject boxes sitting on a non-canonical contract instance.
    off_contract = _boxes_off_canonical_contract(boxes)
    if off_contract:
        logger(f"Reputation proof {token_id} has boxes off the canonical contract; rejecting.")
        return False

    if not all(_validate_box_structure(box) for box in boxes):
        logger("Structural validation of the reputation profile failed.")
        return False

    # Peer ownership challenge: every box must declare the SAME R7 owner, and the peer must
    # prove control of it by signing a fresh challenge with the matching key.
    owners = {
        _decode_coll_byte_hex(str(_extract_register_value(box, "R7") or ""))
        for box in boxes
    }
    owners.discard(None)
    if len(owners) != 1:
        logger(f"Reputation proof {token_id} has inconsistent R7 owners: {sorted(o for o in owners if o)}")
        return False

    owner_proposition_hex = next(iter(owners))
    if not _challenge_peer_ownership(peer_id, owner_proposition_hex):
        logger(f"Peer {peer_id} failed the R7 ownership challenge for proof {token_id}.")
        return False

    return True


"""
    Firma un reto de propiedad con WALLET_MNEMONIC para demostrarle a un tercero
    que este nodo controla exactamente esos propositionBytes (raw ErgoTree).
"""
def sign_message(proposition_bytes, message) -> Optional[str]:
    mnemonic_phrase = ConfigManager().get("ledgers.ergo.WALLET_MNEMONIC")
    if not mnemonic_phrase:
        logger("Missing wallet mnemonic configuration")
        return None

    # Normalize the challenged proposition bytes to raw bytes.
    if isinstance(proposition_bytes, str):
        try:
            proposition_bytes = bytes.fromhex(proposition_bytes.strip())
        except ValueError:
            logger("SignPublicKey: proposition_bytes is not valid hex.")
            return None
    else:
        proposition_bytes = bytes(proposition_bytes or b"")

    if not proposition_bytes:
        logger("SignPublicKey: empty proposition_bytes.")
        return None

    # Sign ONLY when the challenged proposition bytes exactly match the raw propositionBytes
    # derived from the local wallet. Byte equality (==), never identity (`is`).
    local_proposition = owner_proposition_bytes(get_public_key(mnemonic_phrase=mnemonic_phrase))
    if proposition_bytes != local_proposition:
        logger("SignPublicKey: challenged proposition bytes are not mine; refusing to sign.")
        return None

    if isinstance(message, (bytes, bytearray)):
        message = bytes(message).decode("utf-8")

    signed_msg = bip_ecdsa_sign(mnemonic_phrase=mnemonic_phrase, message=message)
    logger("Ownership challenge signed for the local wallet propositionBytes.")
    return signed_msg


"""
    Verifica que el Perfil de Reputación y la cartera del nodo están relacionados
    (el perfil pertenece a la única cartera configurada).
"""
def validate_reputation_proof_ownership(proof_id: Optional[str] = None) -> bool:
    config = ConfigManager()
    mnemonic_phrase = config.get("ledgers.ergo.WALLET_MNEMONIC")
    if proof_id is None:
        proof_id = config.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID")
    return _validate_reputation_proof_ownership(mnemonic_phrase=mnemonic_phrase, proof_id=proof_id)


def _validate_reputation_proof_ownership(mnemonic_phrase: str, proof_id: str) -> bool:
    """Internal helper kept explicit so tests can pin a specific mnemonic/proof pair."""
    if not proof_id:
        logger('Missing reputation proof id on configuration, run submit reputation.')
        return False

    if not mnemonic_phrase:
        logger("Missing mnemonic while validating reputation proof ownership")
        return False

    try:
        # Owner (raw propositionBytes) derived from the single wallet mnemonic.
        # NOTE: get_public_key builds an ErgoAppKit against ledgers.ergo.NODE_URL,
        # so this line needs the Ergo node too — and it runs BEFORE any box is
        # read. An outage surfaces here first, which is why the handler below
        # cannot treat an unexpected error as a verdict.
        address = get_public_key(mnemonic_phrase=mnemonic_phrase)
        expected_owner = owner_proposition_bytes_hex(address)

        boxes = _get_unspent_boxes_by_token(proof_id)
        if not boxes:
            logger(f"No boxes found for proof id {proof_id}")
            return False

        off_contract = _boxes_off_canonical_contract(boxes)
        if off_contract:
            logger(
                f"Reputation proof {proof_id} is not on the canonical contract "
                f"(expected ErgoTree {REPUTATION_PROOF_ERGO_TREE[:16]}…, "
                f"found {[t[:16] + '…' for t in off_contract]}); rejecting."
            )
            return False

        # R7 of a Reputation Box holds the owner's raw propositionBytes (Coll[Byte]).
        box_owners = {
            _decode_coll_byte_hex(str(_extract_register_value(box, "R7") or ""))
            for box in boxes
        }

        valid: bool = box_owners == {expected_owner}
        if not valid:
            logger(
                f"Validation failed: expected owner propositionBytes {expected_owner}, "
                f"found R7 values {sorted([h for h in box_owners if h])}"
            )
        return valid
    except ProofLookupUnavailable:
        # UNDETERMINED, not "not yours": let the caller decide, since the ones
        # here drop config or mint a new proof on a False.
        raise
    except Exception as e:
        # Same reasoning, for anything else that went wrong: AppKit unable to
        # reach the node while deriving the wallet identity, an unexpected
        # response shape, a JVM problem… None of those are the chain telling us
        # the proof is not ours. Only a completed check may return False, because
        # False is what makes callers delete config or mint a new proof.
        raise ProofLookupUnavailable(
            f"Could not validate ownership of reputation proof {proof_id}: {e}"
        ) from e


def _search_boxes_by_r7(ergo, contract_address: str, owner_proposition_hex: str) -> Optional[List[dict]]:
    """
    Try to fetch only the boxes whose R7 equals ``owner_proposition_hex`` using the
    explorer's register-filtered search, avoiding a full paginated scan of the contract.

    Returns the matching boxes, or ``None`` when the endpoint does not support register
    filtering (so the caller falls back to the paginated scan).
    """
    import requests

    api_url = str(ergo.get_api_url()).rstrip("/")
    url = f"{api_url}/api/v1/boxes/unspent/search"
    body = {
        "ergoTreeTemplateHash": None,
        "registers": {"R7": "0e" + format(len(owner_proposition_hex) // 2, "02x") + owner_proposition_hex},
        "constants": {},
        "assets": [],
    }
    try:
        response = requests.post(url, json=body, params={"limit": 50, "offset": 0}, timeout=30)
    except requests.RequestException as e:
        logger(f"R7-filtered search unavailable ({e}); falling back to paginated scan.")
        return None
    if response.status_code != 200:
        logger(f"R7-filtered search returned HTTP {response.status_code}; falling back to paginated scan.")
        return None
    try:
        items = response.json().get("items", [])
    except ValueError:
        return None
    # Filter defensively client-side: the endpoint matches on address, not always R7.
    return [
        b for b in items
        if _decode_coll_byte_hex(str(_extract_register_value(b, "R7") or "")) == owner_proposition_hex
    ]


def __find_reputation_proof_id_for_owner(mnemonic_phrase: str) -> Optional[str]:
    """
    Look up an on-chain reputation proof owned by the given wallet.

    Prefers an R7/propositionBytes-filtered query; only when the endpoint lacks register
    filtering does it fall back to the bounded paginated scan of the canonical contract
    address (with the existing pagination/timeout/log limits). Returns the proof (token) id
    of the first box whose R7 equals the wallet's owner propositionBytes, or None.
    """
    node_url = ConfigManager().get("ledgers.ergo.NODE_URL")
    if not node_url:
        raise ValueError("Missing configuration: ledgers.ergo.NODE_URL")

    ensure_ergpy_jvm(feature="Ergo reputation")
    appkit = require_java_module("ergpy.appkit", feature="Ergo reputation")
    ergo = appkit.ErgoAppKit(node_url=node_url)

    owner_proposition = owner_proposition_bytes_hex(get_public_key(mnemonic_phrase=mnemonic_phrase))
    node_type_nft = ConfigManager().get("ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID") or ""
    contract_address = REPUTATION_PROOF_ADDRESS

    # Fast path: register-filtered lookup.
    filtered = _search_boxes_by_r7(ergo, contract_address, owner_proposition)
    if filtered is not None:
        for box in filtered:
            token_id = _node_own_proof_token_id(box, owner_proposition, node_type_nft)
            if token_id:
                return token_id
        return None

    # Fallback: bounded paginated scan, breaking on first match.
    for box in iter_unspent_boxes_by_address(ergo, contract_address):
        token_id = _node_own_proof_token_id(box, owner_proposition, node_type_nft)
        if token_id:
            return token_id

    return None


def sync_reputation_proof_ownership() -> bool:
    """
    Reconcile the locally configured reputation proof with the wallet mnemonic and report
    every step to the user.
    """
    config = ConfigManager()
    mnemonic_phrase = config.get("ledgers.ergo.WALLET_MNEMONIC")
    proof_id = config.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID")

    if not mnemonic_phrase:
        # Without a wallet there is no identity to reconcile against, so there is
        # no ground to drop a configured proof id either.
        _msg = (
            "No wallet mnemonic is configured; skipping the reputation proof "
            "reconciliation and leaving the node configuration untouched."
        )
        print(_msg, flush=True)
        logger(_msg)
        return False

    try:
        is_valid = validate_reputation_proof_ownership(proof_id=proof_id)
    except ProofLookupUnavailable as e:
        # Nothing was verified, so nothing is reconciled: clearing the proof id
        # here would lose it over a node outage, and the node would go back to
        # advertising no reputation proof at all.
        _msg = (
            f"Could not check reputation proof {proof_id} against the chain ({e}); "
            "leaving the node configuration untouched."
        )
        print(_msg, flush=True)
        logger(_msg)
        return False

    if is_valid:
        print(f"Reputation proof {proof_id} is valid for the configured wallet.", flush=True)
    elif not proof_id:
        # Nothing configured yet: go straight to the on-chain lookup below. Saying
        # "not owned by the configured wallet" here (with an empty id) only made
        # this state harder to read in the logs.
        print("No reputation proof configured; looking one up on-chain.", flush=True)
    else:
        _msg = (
            f"Reputation proof {proof_id} is not owned by the configured wallet; "
            "removing it from the node configuration."
        )
        print(_msg, flush=True)
        logger(_msg)
        config.set("ledgers.ergo.reputation.REPUTATION_PROOF_ID", "")
        proof_id = None

    if proof_id:
        return True

    else:
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
            config.set("ledgers.ergo.reputation.REPUTATION_PROOF_ID", discovered_proof_id)
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
