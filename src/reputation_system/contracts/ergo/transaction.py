import json
from typing import List, Optional, Tuple, TypedDict

import requests

from src.reputation_system.envs import REPUTATION_PROOF_ADDRESS
from src.utils.config import ConfigManager
from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module
from src.utils.logger import LOGGER
from src.utils.network import resolve_public_port


# Constants
env_manager = ConfigManager()
ERGO_NODE_URL = lambda: env_manager.get("ledgers.ergo.NODE_URL")
SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF = lambda: env_manager.get('SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF')
DEFAULT_FEE = 1_000_000
SAFE_MIN_BOX_VALUE = 1_000_000
DEFAULT_TOKEN_AMOUNT = int(env_manager.get('ledgers.ergo.reputation.TOTAL_REPUTATION_TOKEN_AMOUNT'))
PLAIN_TEXT_TYPE_NFT_ID = env_manager.get(
    "ledgers.ergo.reputation.PLAIN_TEXT_TYPE_NFT_ID",
    "",
)
CELAUT_NODE_TYPE_NFT_ID = env_manager.get(
    "ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID",
    "",
)


class ProofObject(TypedDict):
    type: str
    value: str


def __input_box_to_dict(input_box) -> dict:
    return json.loads(str(input_box.toJson(True)))


def _java_bytes_to_python_bytes(java_bytes) -> bytes:
    return bytes((byte + 256) % 256 for byte in java_bytes)


def _owner_proposition_bytes(sender_address) -> bytes:
    from src.reputation_system.contracts.ergo.utils import owner_proposition_bytes

    return owner_proposition_bytes(sender_address)


def _looks_like_hex(value: str) -> bool:
    """True when ``value`` is an even-length string of hex digits (a token id / hash)."""
    if not value or len(value) % 2 != 0:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


def _id_bytes(value: str) -> bytes:
    """
    Encode a register payload the way the whole reputation ecosystem expects: token
    ids / hex pointers are stored as their *raw bytes* (never as UTF-8 text of the
    hex string), and only genuine free-text pointers fall back to UTF-8. Mirrors the
    web app's ``hexOrUtf8ToBytes``. An empty string encodes to an empty Coll[Byte].
    """
    if _looks_like_hex(value):
        return bytes.fromhex(value)
    return (value or "").encode("utf-8")


def _java_bytes(jpype, data: bytes):
    """Wrap Python ``bytes`` (0–255) as a Java signed ``byte[]`` for ErgoValue.of."""
    return jpype.JArray(jpype.JByte)(data)


def _attach_data_inputs(tx_builder, data_inputs) -> None:
    for method_name in ("withDataInputs", "addDataInputs"):
        method = getattr(tx_builder, method_name, None)
        if not method:
            continue

        call_attempts = [
            lambda: method(data_inputs),
            lambda: method(*data_inputs),
        ]
        for attempt in call_attempts:
            try:
                attempt()
                return
            except TypeError:
                continue

    raise RuntimeError("AppKit transaction builder does not expose withDataInputs/addDataInputs")


def _build_unsigned_transaction(ergo, input_boxes, outputs, fee: int, sender_address, data_inputs: Optional[list] = None):
    tx_kwargs = dict(
        input_box=input_boxes,
        outBox=outputs,
        fee=fee / 10**9,
        sender_address=sender_address,
    )

    if not data_inputs:
        return ergo.buildUnsignedTransaction(**tx_kwargs)

    # Prefer the higher-level ergpy API when available.
    for kwarg in ("dataInput", "dataInputs"):
        try:
            return ergo.buildUnsignedTransaction(**tx_kwargs, **{kwarg: data_inputs})
        except TypeError:
            pass

    tx_builder = ergo._ctx.newTxBuilder().boxesToSpend(input_boxes).outputs(outputs).fee(fee).sendChangeTo(sender_address.asP2PK())
    _attach_data_inputs(tx_builder, data_inputs)
    return tx_builder.build()


def __build_proof_box(
    ergo,
    proof_id: str,
    sender_address,
    token_amount: int = DEFAULT_TOKEN_AMOUNT,
    assigned_object: Optional[ProofObject] = None,
    data: str = ""
):
    jpype = require_java_module("jpype", feature="Ergo reputation")
    org_appkit = jpype.JPackage("org").ergoplatform.appkit
    LOGGER(f"Building proof box with token amount {token_amount}")
    type_nft_id = str(assigned_object['type']) if assigned_object else PLAIN_TEXT_TYPE_NFT_ID
    object_to_assign = str(assigned_object['value']) if assigned_object else ""

    owner_proposition = _owner_proposition_bytes(sender_address)
    try:
        ergo_token_cls = org_appkit.ErgoToken
    except AttributeError:
        ergo_token_cls = jpype.JPackage("org").ergoplatform.sdk.ErgoToken

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([ergo_token_cls(proof_id, jpype.JLong(abs(int(token_amount))))]) \
                .registers([
                    org_appkit.ErgoValue.of(_java_bytes(jpype, _id_bytes(type_nft_id))),            # R4: typeNftTokenId (raw bytes)
                    org_appkit.ErgoValue.of(_java_bytes(jpype, _id_bytes(object_to_assign))),       # R5: uniqueObjectData (raw bytes; self = own token id)
                    org_appkit.ErgoValue.of(jpype.JBoolean(False)),                                  # R6: isLocked
                    org_appkit.ErgoValue.of(_java_bytes(jpype, owner_proposition)),                  # R7: raw propositionBytes of the owner
                    org_appkit.ErgoValue.of(jpype.JBoolean(int(token_amount) >= 0)),                # R8: customFlag (sign of the amount)
                    org_appkit.ErgoValue.of(jpype.JString(data).getBytes("utf-8"))                 # R9: content
                ]) \
                .contract(org_appkit.Address.create(REPUTATION_PROOF_ADDRESS).toErgoContract()) \
                .build()


NO_NETWORK_ADDRESS = "No IP available."


def _self_network_data() -> str:
    """Signed ``Peer`` JSON describing how to reach this node, for its self-pointing object.

    A ``Peer``, not a bare ``Instance``: the envelope carries the node's identity
    (``public_key``), the ``signature`` over its addresses, the anti-replay ``ts``
    and the address-expiry estimate -- so a reader gets from the ledger the same
    self-verifying claim GetPeerInfo serves, rather than an unattributed list of
    addresses (issue #236).

    It is verifiable *against this very box*: R7 holds the owner propositionBytes,
    which are ``0008cd`` + the same public key (there is one mnemonic per node), so a
    reader can check the R9 signature against R7 without contacting the node at all.
    That is what makes the published expiry trustworthy -- otherwise whoever relays
    the data could stretch or strip it.

    Deliberately minimal: only the gateway URI. The payment contracts, rates and
    reputation proofs are served by GetPeerInfo (and the proof is this box), and an
    Ergo register is not the place to grow unbounded. The signature covers exactly
    this minimal object, so it verifies as published.
    """
    if not SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF():
        return NO_NETWORK_ADDRESS

    import time

    from google.protobuf.json_format import MessageToJson

    from protos import celaut_pb2
    from src.reputation_system.node_identity import (
        canonical_peer_content_digest,
        canonical_peer_payload,
        declare_signature_scheme,
        get_node_public_key_hex,
        sign_peer_payload,
    )
    from src.utils.network import get_local_ip, resolve_public_host, uri_expiry

    try:
        outbound_ip = get_local_ip()
    except Exception as e:
        LOGGER(f"Could not resolve the outbound IP for the reputation proof: {e}")
        outbound_ip = None

    host = resolve_public_host(
        configured=str(env_manager.get("network.PUBLIC_IP", "") or ""),
        outbound_ip=outbound_ip,
    )
    if not host:
        LOGGER("No public address to advertise (set network.PUBLIC_IP if the node is behind NAT).")
        return NO_NETWORK_ADDRESS

    internal_port = env_manager.get_gateway_port()
    public_port = resolve_public_port(env_manager.get("network.PUBLIC_TCP_PORT", ""), internal_port)
    peer = celaut_pb2.Peer()
    uri = peer.uri.add()
    uri.ip = host
    uri.port = public_port
    uri.transport.tags.append("tcp")

    public_key_hex = get_node_public_key_hex()
    if public_key_hex:
        ts = int(time.time())
        uri.expiry_unix_timestamp = uri_expiry(ts)
        signature = sign_peer_payload(
            canonical_peer_payload(
                public_key_hex, ts, canonical_peer_content_digest(peer),
            )
        )
        if signature:
            peer.public_key = public_key_hex
            peer.signature = signature
            peer.ts = ts
            # This one is read off the ledger by people who never contacted the node,
            # so it is the announcement that most needs to say which cryptography it
            # is asking them to verify -- but the tags say it. Spelling the scheme out
            # in prose as well would be a third of this register, paid for in storage
            # rent by every box that carries it.
            declare_signature_scheme(peer, prose=False)
    else:
        LOGGER("No node identity available; publishing the address unsigned.")

    LOGGER(f"Advertising {host}:{public_port} on the reputation proof.")
    return MessageToJson(peer)


def __create_reputation_proof_tx(node_url: str, wallet_mnemonic: str, proof_id: Optional[str], objects: List[Tuple[Optional[str], int, Optional[str]]]):
    ensure_ergpy_jvm(feature="Ergo reputation")
    appkit = require_java_module("ergpy.appkit", feature="Ergo reputation")
    jpype = require_java_module("jpype", feature="Ergo reputation")
    org_appkit = jpype.JPackage("org").ergoplatform.appkit
    from src.reputation_system.contracts.ergo.proof_validation import validate_reputation_proof_ownership
    from src.reputation_system.contracts.ergo.utils import get_public_key, get_boxes_by_token_ids

    ergo = appkit.ErgoAppKit(node_url=node_url)
    fee = DEFAULT_FEE
    safe_min_out_box = (len(objects) + 1) * SAFE_MIN_BOX_VALUE

    sender_address = get_public_key(wallet_mnemonic)
    LOGGER(f"Sender address -> {sender_address.toString()}")

    wallet_input_boxes = ergo.getInputBoxCovering(amount_list=[fee], sender_address=sender_address)
    selected_input_box = min(
        (input_box for input_box in wallet_input_boxes if __input_box_to_dict(input_box)["value"] > safe_min_out_box),
        key=lambda ib: __input_box_to_dict(ib)["value"],
        default=None
    )
    if not selected_input_box:
        raise Exception("No input box available.")

    external_token_value = int(sum([obj[1] for obj in objects if obj[0]]))
    expected_total_reputation = int(env_manager.get('ledgers.ergo.reputation.TOTAL_REPUTATION_TOKEN_AMOUNT'))

    if not external_token_value:
        is_self = any(obj[0] for obj in objects)
        num = len(objects) if not is_self else len(objects) - 1
        total = expected_total_reputation if not is_self else expected_total_reputation - 1
        objects = [
            (obj[0], int(total / num), obj[2]) if obj[0] else (obj[0], obj[1], obj[2])
            for obj in objects
        ]

    total_token_value = int(sum([obj[1] for obj in objects]))
    assert expected_total_reputation == total_token_value, (
        f"The sum of the values to be spent must equal the total reputation token amount ({expected_total_reputation}) and not {total_token_value}")

    input_boxes = [selected_input_box]

    if proof_id and not validate_reputation_proof_ownership(proof_id=proof_id):
        LOGGER(f"The reputation proof ID {proof_id} is not associated with the current Ergo wallet mnemonic and will be removed.")
        proof_id = None

    if proof_id:
        try:
            # Spend the existing proof from the canonical ecosystem contract address
            # (ErgoTree v1), not a locally-recompiled ErgoTree v0 that would sit at a
            # different, ecosystem-invisible address.
            script_address = org_appkit.Address.create(REPUTATION_PROOF_ADDRESS)
            input_list = ergo.getInputBoxCovering(
                amount_list=[SAFE_MIN_BOX_VALUE],
                sender_address=script_address,
                tokenList=[[proof_id]],
                amount_tokens=[[total_token_value]],
            )
            input_boxes.extend([
                box for box in input_list
                if (
                    isinstance(__input_box_to_dict(box), dict)
                    and 'assets' in __input_box_to_dict(box)
                    and isinstance(__input_box_to_dict(box)['assets'], list)
                    and len(__input_box_to_dict(box)['assets']) > 0
                    and __input_box_to_dict(box)['assets'][0].get('tokenId') == proof_id
                )
            ])
        except Exception as e:
            LOGGER(f"Exception submitting with the last proof_id: {str(e)}. A new one will be generated.")
            proof_id = None

    java_input_boxes = jpype.java.util.ArrayList(input_boxes)
    proof_id = proof_id or str(java_input_boxes.get(0).getId().toString())

    value_in_nanoergs = (__input_box_to_dict(selected_input_box)["value"] - fee - safe_min_out_box)
    assert value_in_nanoergs >= SAFE_MIN_BOX_VALUE, (
        f"Value in nanoergs ({value_in_nanoergs}) must be greater than SAFE_MIN_BOX_VALUE ({SAFE_MIN_BOX_VALUE})")
    value_in_ergs = value_in_nanoergs / 10**9

    outputs = []

    # An opinion is about a node, and a node *is* its public key (issue #236), so R5
    # carries the target's key. It used to carry a reputation proof's token id, which
    # made every opinion an opinion about one of that node's proofs rather than about
    # the node -- a single key can hold several proofs, and minting a fresh one shed
    # the accumulated on-chain reputation (issue #281). Our own key comes from the same
    # identity keypair the R7 owner does, so a self-opinion is addressed exactly like
    # any peer's.
    from src.reputation_system.node_identity import get_node_public_key_hex

    node_public_key = get_node_public_key_hex()
    if not node_public_key:
        raise Exception(
            "No node identity public key available (ledgers.ergo.WALLET_MNEMONIC); "
            "cannot address a reputation opinion."
        )

    for obj in objects:
        self_info = not obj[0]
        if self_info:
            data = _self_network_data()
        else:
            data = obj[2]

        proof_box = __build_proof_box(
            ergo=ergo,
            proof_id=proof_id,
            sender_address=sender_address,
            assigned_object=ProofObject(
                type=CELAUT_NODE_TYPE_NFT_ID,
                value=node_public_key if self_info else obj[0]
            ),
            token_amount=int(obj[1]),
            data=data or ""
        )
        outputs.append(proof_box)

    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[sender_address.toString()], amount_list=[value_in_ergs])
    outputs.extend(output_boxes)

    # Resolve and attach DPG type boxes as dataInputs.
    java_data_inputs = get_boxes_by_token_ids(
        ergo=ergo,
        node_url=node_url,
        token_ids=[CELAUT_NODE_TYPE_NFT_ID],
    )
    unsigned_tx = _build_unsigned_transaction(
        ergo=ergo,
        input_boxes=java_input_boxes,
        outputs=outputs,
        fee=fee,
        sender_address=sender_address,
        data_inputs=java_data_inputs,
    )

    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=None)
    signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index=0)
    tx_id = ergo.txId(signed_tx)

    if env_manager.get('ledgers.ergo.reputation.REPUTATION_PROOF_ID') != proof_id:
        LOGGER(f"Store reputation proof id {proof_id} on config file.")
        env_manager.set("ledgers.ergo.reputation.REPUTATION_PROOF_ID", proof_id)

    return tx_id


def submit_reputation_proof(objects: List[Tuple[str, int, str]]) -> bool:
    try:
        from src.payment_system.contracts.ergo.interface import get_amount_by_addr

        proof_id = env_manager.get('ledgers.ergo.reputation.REPUTATION_PROOF_ID')
        mnemonic = env_manager.get('ledgers.ergo.WALLET_MNEMONIC') or env_manager.get('WALLET_MNEMONIC')
        node_url = ERGO_NODE_URL()

        if not node_url:
            LOGGER("Missing configuration: ledgers.ergo.NODE_URL")
            return False

        if not mnemonic:
            LOGGER("Missing configuration: ledgers.ergo.WALLET_MNEMONIC")
            return False

        if get_amount_by_addr(mnemonic=mnemonic) <= DEFAULT_FEE:
            LOGGER("There are not enough nanoErgs to upload the reputation proof to the network.")
            return False

        LOGGER(f"Submitting reputation proof with {len(objects)} objects.")
        tx_id = __create_reputation_proof_tx(
            node_url=node_url,
            wallet_mnemonic=mnemonic,
            proof_id=proof_id,
            objects=objects,
        )
        LOGGER(f"Submitted tx -> {tx_id}")
        return tx_id is not None
    except Exception as e:
        LOGGER(f"Exception submitting reputation proof: {str(e)}")
        return False
