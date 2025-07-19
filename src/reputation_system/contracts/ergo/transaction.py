from src.database.sql_connection import TOTAL_REPUTATION_TOKEN_AMOUNT
import json
from google.protobuf.json_format import MessageToJson
from ergpy import appkit
from ergpy.helper_functions import initialize_jvm
import jpype
from enum import Enum
from typing import List, TypedDict, Optional, Tuple

from src.payment_system.contracts.ergo.interface import get_amount_by_addr
from src.reputation_system.contracts.ergo.utils import get_public_key
from src.reputation_system.envs import CONTRACT
from src.reputation_system.contracts.ergo.proof_validation import validate_reputation_proof_ownership
from src.tunneling_system.tunnels import TunnelSystem
from src.utils.logger import LOGGER
from src.utils.config import ConfigManager

from jpype import *
import java.lang

from java.lang import Boolean, Long
from java.math import BigInteger

from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *

sigmastate = JPackage('sigmastate')


# Constants
env_manager = ConfigManager()
ERGO_NODE_URL = lambda: env_manager.get("ledgers.ergo.NODE_URL")
SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF = env_manager.get('SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF')
DEFAULT_FEE = 1_000_000
SAFE_MIN_BOX_VALUE = 1_000_000
DEFAULT_TOKEN_AMOUNT = env_manager.get('TOTAL_REPUTATION_TOKEN_AMOUNT')

# Enum definitions
class ProofObjectType(Enum):
    PlainText = "8299d98e15ebee7fa39ad716de7c8bb191790a1bf4b7c3f91af35a0e36187706"
    CelautNode = "64060577c3393e0e3cf8938ec8e6a2002ded27ece17750aa5add7d5c3e1227ba"

# TypedDict definitions
class ProofObject(TypedDict):
    type: ProofObjectType
    value: str

# Utility function to convert InputBox to dict
def __input_box_to_dict(input_box: 'org.ergoplatform.appkit.InputBoxImpl') -> dict:
    return json.loads(str(input_box.toJson(True)))

# Function to build the proof box
def __build_proof_box(
    ergo: appkit.ErgoAppKit,
    proof_id: str,
    sender_address: Address,
    token_amount: int = DEFAULT_TOKEN_AMOUNT,
    assigned_object: Optional[ProofObject] = None,
    data: str = ""
):
    LOGGER(f"Building proof box with token amount {token_amount}")
    type_nft_id = assigned_object['type'] if assigned_object else ProofObjectType.PlainText
    object_to_assign = assigned_object['value'] if assigned_object else ""

    # ergoTree = sender_address.getErgoAddress().script()
    # sender_address_proposition = sigmastate.serialization.ErgoTreeSerializer.DefaultSerializer().serializeErgoTree(ergoTree)
    p2pkAddres = sender_address.asP2PK()
    sender_address_proposition = p2pkAddres.pubkey()

    LOGGER(f"Sender address proposition: {sender_address_proposition}")

    tuple_values = jpype.JClass("scala.Tuple2")(
        Boolean.FALSE,
        Long.valueOf(TOTAL_REPUTATION_TOKEN_AMOUNT)
    )
    pair_type = ErgoType.pairType(ErgoType.booleanType(), ErgoType.longType())

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([ErgoToken(proof_id, jpype.JLong(abs(token_amount)))]) \
                .registers([
                    ErgoValue.of(jpype.JString(type_nft_id.value).getBytes("utf-8")),                                   # R4
                    ErgoValue.of(jpype.JString(object_to_assign).getBytes("utf-8")),                                    # R5
                    ErgoValue.of(tuple_values, pair_type),                                                               # R6
                    ErgoValue.of(sender_address_proposition),                                                           # R7
                    ErgoValue.of(jpype.JBoolean(token_amount >= 0)),                                                    # R8
                    ErgoValue.of(jpype.JString(data).getBytes("utf-8"))                                                 # R9   JSON celaut.Instance
                ]) \
                .contract(ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)) \
                .build()

@initialize_jvm
def __create_reputation_proof_tx(node_url: str, wallet_mnemonic: str, proof_id: str, objects: List[Tuple[Optional[str], int, Optional[str]]]):
    ergo = appkit.ErgoAppKit(node_url=node_url)
    fee = DEFAULT_FEE  # Fee in nanoErgs
    safe_min_out_box = (len(objects)+1) * SAFE_MIN_BOX_VALUE

    # 1. Get the change address
    sender_address = get_public_key(wallet_mnemonic)

    LOGGER(f"Sender address -> {sender_address.toString()}")

    # 2. Prepare transaction inputs (get UTXOs)
    wallet_input_boxes = ergo.getInputBoxCovering(amount_list=[fee], sender_address=sender_address)

    # Select the input box with min value to avoid NotEnoughErgsError
    selected_input_box = min(
        (input_box for input_box in wallet_input_boxes if __input_box_to_dict(input_box)["value"] > safe_min_out_box),
        key=lambda ib: __input_box_to_dict(ib)["value"],
        default=None
    )

    if not selected_input_box:
        raise Exception("No input box available.")

    external_token_value = int(sum([obj[1] for obj in objects if obj[0]])) # type: ignore  # In case of self reputation, the value is not divided.
    _expected_total_reputation = env_manager.get('TOTAL_REPUTATION_TOKEN_AMOUNT')
    
    if not external_token_value:
        # If all the objects have a percentage of 0, then the total reputation value must be divided equally.  This is because division by zero was avoided on the sql function.
        _is_self = any(obj[0] for obj in objects)
        _num = len(objects) if not _is_self else len(objects) - 1
        _total = _expected_total_reputation if not _is_self else _expected_total_reputation - 1

        objects = [
            (obj[0], _total / _num, obj[2]) if obj[0] else (obj[0], obj[1], obj[2])
            for obj in objects
        ]
    
    total_token_value = int(sum([obj[1] for obj in objects]))
                
    assert _expected_total_reputation == total_token_value, (
        f"The sum of the values to be spent must equal the total reputation token amount ({_expected_total_reputation}) and not {total_token_value}")
    
    LOGGER(f"Needs to be spent {total_token_value} reputation value.")
    input_boxes = [selected_input_box]
    
    # Validates reputation proof.
    if not validate_reputation_proof_ownership():
        LOGGER(f"The reputation proof ID {proof_id} is not associated with the current Ergo wallet mnemonic and will be removed.")
        proof_id = ""
    
    LOGGER(f"Using proof id -> {proof_id}")
    if proof_id:
        try:
            _contract = ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)
            _ergo_tree = _contract.getErgoTree()
            _token_id = Address.fromErgoTree(_ergo_tree, NetworkType.MAINNET)
            input_list = ergo.getInputBoxCovering(
                amount_list=[SAFE_MIN_BOX_VALUE],
                sender_address=_token_id,
                tokenList=[[proof_id]], amount_tokens=[[total_token_value]]  # TODO this filter don't work. (Where proof_id is a token_id)  https://discord.com/channels/668903786361651200/669989266478202917/1283108062324064256
            )
            input_boxes.extend([
                _i for _i in input_list
                if (
                    isinstance(__input_box_to_dict(_i), dict)  # Check if the result is a dictionary
                    and 'assets' in __input_box_to_dict(_i)  # Ensure the 'assets' key exists
                    and isinstance(__input_box_to_dict(_i)['assets'], list)  # Check if 'assets' is a list
                    and len(__input_box_to_dict(_i)['assets']) > 0  # Ensure the list is not empty
                    and 'tokenId' in __input_box_to_dict(_i)['assets'][0]  # Check that 'tokenId' exists in the first item
                    and __input_box_to_dict(_i)['assets'][0]['tokenId'] == proof_id  # Compare 'tokenId' with proof_id
                )
            ])
            del input_list
        except Exception as e:
            LOGGER(f"Exception submitting with the last proof_id: {str(e)}.\n A new one will be generated.")
            proof_id = None

    java_input_boxes = java.util.ArrayList(input_boxes)

    LOGGER(f"selected_input_box value: {__input_box_to_dict(selected_input_box)['value']}")
    LOGGER(f"fee: {fee}, SAFE_MIN_BOX_VALUE: {safe_min_out_box}")
    
    value_in_nanoergs = (__input_box_to_dict(selected_input_box)["value"] - fee - safe_min_out_box)
    assert value_in_nanoergs >= SAFE_MIN_BOX_VALUE, f"Value in nanoergs ({value_in_nanoergs}) must be greater than SAFE_MIN_BOX_VALUE ({SAFE_MIN_BOX_VALUE})"
    value_in_ergs = value_in_nanoergs  / 10**9
    LOGGER(f"value in ergs to be spent: {value_in_ergs}")

    # 3. Build transaction outputs
    outputs = []

    # Check reputation proof id.
    try:
        proof_id = proof_id if proof_id else java_input_boxes.get(0).getId().toString()  # Assume that, if it is not an empty string, the proof ID corresponds to an existing token ID.
    except Exception as e:
        LOGGER(f"Exception getting proof id: {str(e)}.")
        raise e

    LOGGER(f"proof id -> {proof_id}")

    # Reputation proof output box
    for obj in objects:
        self_info = not obj[0]  # if obj[0] is None, refers to itself.
        if self_info:
            data = "No IP available."
            if SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF:
                try:
                    data = MessageToJson(TunnelSystem().get_gateway_tunnel().instance)
                except Exception as e:
                    LOGGER(f"Exception getting gateway tunnel instance: {str(e)}")
            
        else:
            data = obj[2]
            
        LOGGER(f"Building proof box with data: {data}")
        proof_box = __build_proof_box(
            ergo=ergo,
            proof_id=proof_id,
            sender_address=sender_address,
            assigned_object=ProofObject(
                type=ProofObjectType.CelautNode,
                value=obj[0] if not self_info else proof_id
            ),
            token_amount=obj[1],
            data=data
        )
        if proof_box:
            LOGGER("Proof box built.")
            outputs.append(proof_box)
        else:
            LOGGER(f"Proof box not built for object {obj}")

    LOGGER(f"Builded all proof boxes.")
    # Basic wallet output box
    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[sender_address.toString()], amount_list=[value_in_ergs])
    if not output_boxes: LOGGER(f"No build out boxes.")
    outputs.extend(output_boxes)

    # 4. Build and sign the transaction
    unsigned_tx = ergo.buildUnsignedTransaction(
        # TODO: Add dataInputs [typeNftBox, rest of the proof boxes ...]
        input_box=java_input_boxes,
        outBox=outputs,
        fee=fee / 10**9,
        sender_address=sender_address
    )

    try:
        mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=None)
    except Exception as e:
        LOGGER(f"Error getting mnemonic {wallet_mnemonic}\n {str(e)}")
        raise e
    
    try:
        signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index=0)
    except Exception as e:
        LOGGER(f"Error signing transaction: {str(e)}\n Unsigned transaction: {unsigned_tx} \n mnemonic: {mnemonic}")
        raise e

    # 5. Submit the transaction and return the ID
    tx_id = ergo.txId(signed_tx)

    if env_manager.get('REPUTATION_PROOF_ID') != proof_id:
        LOGGER(f"Store reputation proof id {proof_id} on .env file.")
        env_manager.write_env("REPUTATION_PROOF_ID", proof_id)
        if env_manager.get('REPUTATION_PROOF_ID') != proof_id:
            LOGGER(f"Proof ID was not stored correctly: {env_manager.get('REPUTATION_PROOF_ID')} != {proof_id}")
    return tx_id

def submit_reputation_proof(objects: List[Tuple[str, int, str]]) -> bool:
    try:
        proof_id = env_manager.get('REPUTATION_PROOF_ID')
        mnemonic=env_manager.get('WALLET_MNEMONIC')

        if proof_id:
            LOGGER(f"Not supported update reputation proofs (https://github.com/celaut-project/nodo/issues/80)")
            return False

        if get_amount_by_addr(mnemonic=mnemonic) <= DEFAULT_FEE:
            LOGGER("There are not enough nanoErgs to upload the reputation proof to the network.")
            return False
        
        LOGGER(f"Submitting reputation proof with {len(objects)} objects.")
        tx_id = __create_reputation_proof_tx(
            node_url=ledgers.ergo.NODE_URL(),
            wallet_mnemonic=mnemonic,
            proof_id=proof_id,
            objects=objects,
        )
        LOGGER(f"Submited tx -> {tx_id}")
        return tx_id != None
    except Exception as e:
        LOGGER(f"Exception submitting reputation proof: {str(e)}")
        return False
