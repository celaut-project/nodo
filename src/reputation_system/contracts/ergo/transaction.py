import hashlib
import json
from typing import List, Optional, Tuple, TypedDict

import jpype
import requests
from ergpy import appkit
from ergpy.helper_functions import initialize_jvm
from google.protobuf.json_format import MessageToJson

from src.payment_system.contracts.ergo.interface import get_amount_by_addr
from src.reputation_system.contracts.ergo.proof_validation import validate_reputation_proof_ownership
from src.reputation_system.contracts.ergo.utils import get_public_key
from src.reputation_system.envs import CONTRACT
from src.tunneling_system.tunnels import TunnelSystem
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

from jpype import JPackage

from org.ergoplatform.appkit import Address, ConstantsBuilder, ErgoToken, ErgoValue, NetworkType


# Constants
env_manager = ConfigManager()
ERGO_NODE_URL = lambda: env_manager.get("ledgers.ergo.NODE_URL")
SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF = env_manager.get('SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF')
DEFAULT_FEE = 1_000_000
SAFE_MIN_BOX_VALUE = 1_000_000
DEFAULT_TOKEN_AMOUNT = int(env_manager.get('TOTAL_REPUTATION_TOKEN_AMOUNT'))
PLAIN_TEXT_TYPE_NFT_ID = env_manager.get(
    "reputation.PLAIN_TEXT_TYPE_NFT_ID",
    "",
)
CELAUT_NODE_TYPE_NFT_ID = env_manager.get(
    "reputation.CELAUT_NODE_TYPE_NFT_ID",
    "",
)


class ProofObject(TypedDict):
    type: str
    value: str


def __input_box_to_dict(input_box: 'org.ergoplatform.appkit.InputBoxImpl') -> dict:
    return json.loads(str(input_box.toJson(True)))


def _java_bytes_to_python_bytes(java_bytes) -> bytes:
    return bytes((byte + 256) % 256 for byte in java_bytes)


def _owner_script_hash(sender_address: Address) -> bytes:
    ergo_tree = sender_address.getErgoAddress().script()
    serializer = JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
    proposition_bytes = _java_bytes_to_python_bytes(serializer.serializeErgoTree(ergo_tree))
    return hashlib.blake2b(proposition_bytes, digest_size=32).digest()


def _get_type_nft_boxes(node_url: str, type_nft_ids: List[str]) -> list:
    if not node_url:
        raise ValueError("Missing configuration: ledgers.ergo.NODE_URL")

    unique_ids = {token_id for token_id in type_nft_ids if token_id}
    if not unique_ids:
        raise ValueError("No type NFT IDs were provided to resolve dataInputs")

    data_inputs = []
    for token_id in unique_ids:
        url = f"{node_url}/api/v1/boxes/byTokenId/{token_id}"
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            raise ValueError(f"Could not fetch Type NFT {token_id}: HTTP {response.status_code}")

        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            raise ValueError(f"Type NFT {token_id} was not found in explorer response")

        data_inputs.append(items[0])

    return data_inputs


def __build_proof_box(
    ergo: appkit.ErgoAppKit,
    proof_id: str,
    sender_address: Address,
    token_amount: int = DEFAULT_TOKEN_AMOUNT,
    assigned_object: Optional[ProofObject] = None,
    data: str = ""
):
    LOGGER(f"Building proof box with token amount {token_amount}")
    type_nft_id = assigned_object['type'] if assigned_object else PLAIN_TEXT_TYPE_NFT_ID
    object_to_assign = assigned_object['value'] if assigned_object else ""

    owner_hash = _owner_script_hash(sender_address)

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([ErgoToken(proof_id, jpype.JLong(abs(int(token_amount))))]) \
                .registers([
                    ErgoValue.of(jpype.JString(type_nft_id).getBytes("utf-8")),         # R4: typeNftTokenId
                    ErgoValue.of(jpype.JString(object_to_assign).getBytes("utf-8")),    # R5: uniqueObjectData
                    ErgoValue.of(jpype.JBoolean(False)),                                  # R6: isLocked
                    ErgoValue.of(owner_hash),                                             # R7: blake2b256(propositionBytes)
                    ErgoValue.of(jpype.JBoolean(int(token_amount) >= 0)),                # R8: positive/negative
                    ErgoValue.of(jpype.JString(data).getBytes("utf-8"))                 # R9: content
                ]) \
                .contract(ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)) \
                .build()


@initialize_jvm
def __create_reputation_proof_tx(node_url: str, wallet_mnemonic: str, proof_id: Optional[str], objects: List[Tuple[Optional[str], int, Optional[str]]]):
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
    expected_total_reputation = int(env_manager.get('TOTAL_REPUTATION_TOKEN_AMOUNT'))

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

    if proof_id and not validate_reputation_proof_ownership():
        LOGGER(f"The reputation proof ID {proof_id} is not associated with the current Ergo wallet mnemonic and will be removed.")
        proof_id = None

    if proof_id:
        try:
            compiled_contract = ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)
            ergo_tree = compiled_contract.getErgoTree()
            script_address = Address.fromErgoTree(ergo_tree, NetworkType.MAINNET)
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
    proof_id = proof_id or java_input_boxes.get(0).getId().toString()

    value_in_nanoergs = (__input_box_to_dict(selected_input_box)["value"] - fee - safe_min_out_box)
    assert value_in_nanoergs >= SAFE_MIN_BOX_VALUE, (
        f"Value in nanoergs ({value_in_nanoergs}) must be greater than SAFE_MIN_BOX_VALUE ({SAFE_MIN_BOX_VALUE})")
    value_in_ergs = value_in_nanoergs / 10**9

    outputs = []

    for obj in objects:
        self_info = not obj[0]
        if self_info:
            data = "No IP available."
            if SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF:
                try:
                    data = MessageToJson(TunnelSystem().get_gateway_tunnel().instance)
                except Exception as e:
                    LOGGER(f"Exception getting gateway tunnel instance: {str(e)}")
        else:
            data = obj[2]

        proof_box = __build_proof_box(
            ergo=ergo,
            proof_id=proof_id,
            sender_address=sender_address,
            assigned_object=ProofObject(
                type=CELAUT_NODE_TYPE_NFT_ID,
                value=obj[0] if not self_info else proof_id
            ),
            token_amount=int(obj[1]),
            data=data or ""
        )
        outputs.append(proof_box)

    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[sender_address.toString()], amount_list=[value_in_ergs])
    outputs.extend(output_boxes)

    # Resolve and attach DPG type boxes as dataInputs.
    data_inputs = _get_type_nft_boxes(
        node_url=node_url,
        type_nft_ids=[CELAUT_NODE_TYPE_NFT_ID],
    )

    tx_kwargs = dict(
        input_box=java_input_boxes,
        outBox=outputs,
        fee=fee / 10**9,
        sender_address=sender_address,
    )

    # API compatibility across ergpy/appkit versions.
    try:
        unsigned_tx = ergo.buildUnsignedTransaction(dataInput=data_inputs, **tx_kwargs)
    except TypeError:
        try:
            unsigned_tx = ergo.buildUnsignedTransaction(dataInputs=data_inputs, **tx_kwargs)
        except TypeError:
            raise RuntimeError("AppKit does not expose dataInput/dataInputs argument; cannot satisfy Type NFT dataInputs requirement")

    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=None)
    signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index=0)
    tx_id = ergo.txId(signed_tx)

    if env_manager.get('REPUTATION_PROOF_ID') != proof_id:
        LOGGER(f"Store reputation proof id {proof_id} on config file.")
        env_manager.set("reputation.REPUTATION_PROOF_ID", proof_id)

    return tx_id


def submit_reputation_proof(objects: List[Tuple[str, int, str]]) -> bool:
    try:
        proof_id = env_manager.get('reputation.REPUTATION_PROOF_ID') or env_manager.get('REPUTATION_PROOF_ID')
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
