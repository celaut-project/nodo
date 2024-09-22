import json
from google.protobuf.json_format import MessageToJson
from ergpy import appkit
from ergpy.helper_functions import initialize_jvm
import jpype
from enum import Enum
from typing import List, TypedDict, Optional, Tuple

from src.gateway.utils import generate_gateway_instance
from src.payment_system.contracts.ergo.interface import ERGO_NODE_URL
from src.utils.logger import LOGGER
from src.utils.env import EnvManager

from jpype import *
import java.lang

from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *

sigmastate = JPackage('sigmastate')


# Constants
env_manager = EnvManager()
DEFAULT_FEE = 1_000_000
SAFE_MIN_BOX_VALUE = 1_000_000
DEFAULT_TOKEN_AMOUNT = env_manager.get_env('TOTAL_REPUTATION_TOKEN_AMOUNT')
DEFAULT_TOKEN_LABEL = "celaut-node"
CONTRACT = """{
    SELF.R7[SigmaProp].get &&
    sigmaProp(SELF.tokens.size == 1) &&
    sigmaProp(OUTPUTS.forall { (x: Box) =>
    !(x.tokens.exists { (token: (Coll[Byte], Long)) => token._1 == SELF.tokens(0)._1 }) ||
    (
        x.R7[SigmaProp].get == SELF.R7[SigmaProp].get &&
        x.tokens.size == 1 &&
        x.propositionBytes == SELF.propositionBytes &&
        (x.R8[Boolean].get == false || x.R8[Boolean].get == true)
    )
    })
}"""

# Enum definitions
class ProofObjectType(Enum):
    PlainText = "plain/txt-utf8"
    ProofByToken = "token-proof"

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
    reputation_token_label: str = DEFAULT_TOKEN_LABEL,
    assigned_object: Optional[ProofObject] = None,
    data: str = ""
):
    object_type_to_assign = assigned_object['type'] if assigned_object else ProofObjectType.PlainText
    object_to_assign = assigned_object['value'] if assigned_object else ""

    # ergoTree = sender_address.getErgoAddress().script()
    # sender_address_proposition = sigmastate.serialization.ErgoTreeSerializer.DefaultSerializer().serializeErgoTree(ergoTree)
    p2pkAddres = sender_address.asP2PK()
    sender_address_proposition = p2pkAddres.pubkey()

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([ErgoToken(proof_id, jpype.JLong(abs(token_amount)))]) \
                .registers([
                    ErgoValue.of(jpype.JString(reputation_token_label).getBytes("utf-8")),         # R4
                    ErgoValue.of(jpype.JString(object_type_to_assign.value).getBytes("utf-8")),    # R5
                    ErgoValue.of(jpype.JString(object_to_assign).getBytes("utf-8")),               # R6
                    ErgoValue.of(sender_address_proposition),                                      # R7
                    ErgoValue.of(jpype.JBoolean(token_amount >= 0)),                               # R8
                    ErgoValue.of(jpype.JString(data).getBytes("utf-8"))                            # R9   JSON celaut.Instance
                ]) \
                .contract(ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)) \
                .build()

@initialize_jvm
def __create_reputation_proof_tx(node_url: str, wallet_mnemonic: str, proof_id: str, objects: List[Tuple[Optional[str], int, Optional[str]]]):
    ergo = appkit.ErgoAppKit(node_url=node_url)
    fee = DEFAULT_FEE  # Fee in nanoErgs
    safe_min_out_box = (len(objects)+1) * SAFE_MIN_BOX_VALUE

    # 1. Get the change address
    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=None)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

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

    total_token_value = int(sum([obj[1] for obj in objects])) # type: ignore
    assert env_manager.get_env('TOTAL_REPUTATION_TOKEN_AMOUNT') == total_token_value, (
        "The sum of the values to be spent must equal the total reputation token amount."
    )
    LOGGER(f"Needs to be spent {total_token_value} reputation value.")
    input_boxes = [selected_input_box]
    LOGGER(f"Using proof id -> {proof_id}")
    if proof_id:
        try:
            _contract = ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)
            _ergo_tree = _contract.getErgoTree()
            _contract_addr = Address.fromErgoTree(_ergo_tree, NetworkType.TESTNET)
            input_list = ergo.getInputBoxCovering(
                amount_list=[SAFE_MIN_BOX_VALUE],
                sender_address=_contract_addr,
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
    value_in_ergs = (__input_box_to_dict(selected_input_box)["value"] + SAFE_MIN_BOX_VALUE - fee - safe_min_out_box) / 10**9

    LOGGER(f"value in ergs to be spent: {value_in_ergs}")

    # 3. Build transaction outputs
    outputs = []

    # Check reputation proof id.
    proof_id = proof_id if proof_id else java_input_boxes.get(0).getId().toString()  # Assume that, if it is not an empty string, the proof ID corresponds to an existing token ID.

    LOGGER(f"proof id -> {proof_id}")

    # Reputation proof output box
    for obj in objects:
        self_info = not obj[0]  # if obj[0] is None, refers to itself.
        proof_box = __build_proof_box(
            ergo=ergo,
            proof_id=proof_id,
            sender_address=sender_address,
            assigned_object=ProofObject(
                type=ProofObjectType.ProofByToken,
                value=obj[0] if self_info else proof_id
            ),
            token_amount=obj[1],
            data=obj[2] if self_info else MessageToJson(generate_gateway_instance(ERGO_NODE_URL).instance)
        )
        if proof_box:
            outputs.append(proof_box)

    LOGGER(f"Builded all proof box boxes.")
    # Basic wallet output box
    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[sender_address.toString()], amount_list=[value_in_ergs])
    if not output_boxes: LOGGER(f"No build out boxes.")
    outputs.extend(output_boxes)

    # 4. Build and sign the transaction
    unsigned_tx = ergo.buildUnsignedTransaction(
        input_box=java_input_boxes,
        outBox=outputs,
        fee=fee / 10**9,
        sender_address=sender_address
    )

    signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index=0)

    # 5. Submit the transaction and return the ID
    tx_id = ergo.txId(signed_tx)

    if env_manager.get_env('REPUTATION_PROOF_ID') != proof_id:
        LOGGER(f"Store reputation proof id {proof_id} on .env file.")
        env_manager.write_env("REPUTATION_PROOF_ID", proof_id)
        if env_manager.get_env('REPUTATION_PROOF_ID') != proof_id:
            LOGGER(f"Proof ID was not stored correctly: {env_manager.get_env('REPUTATION_PROOF_ID')} != {proof_id}")
    return tx_id

def submit_reputation_proof(objects: List[Tuple[str, int, str]]) -> bool:
    try:
        tx_id = __create_reputation_proof_tx(
            node_url=env_manager.get_env('ERGO_NODE_URL'),
            wallet_mnemonic=env_manager.get_env('ERGO_WALLET_MNEMONIC'),
            proof_id=env_manager.get_env('REPUTATION_PROOF_ID'),
            objects=objects,
        )
        LOGGER(f"Submited tx -> {tx_id}")
        return tx_id != None
    except Exception as e:
        LOGGER(str(e))
        return False
