import json
from ergpy import appkit
from ergpy.helper_functions import initialize_jvm
import jpype
from enum import Enum
from typing import List, TypedDict, Optional, Tuple

from src.utils.logger import LOGGER
from src.utils.env import write_env, ERGO_NODE_URL, ERGO_WALLET_MNEMONIC, TOTAL_REPUTATION_TOKEN_AMOUNT, REVIEWER_REPUTATION_PROOF_ID

from jpype import *
import java.lang

from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *

# Constants
DEFAULT_FEE = 1_000_000
SAFE_MIN_BOX_VALUE = 1_000_000
DEFAULT_TOKEN_AMOUNT = TOTAL_REPUTATION_TOKEN_AMOUNT
DEFAULT_TOKEN_LABEL = "reputation-proof-token"
CONTRACT = """{
  proveDlog(SELF.R7[GroupElement].get) &&
  sigmaProp(SELF.tokens.size == 1) &&
  sigmaProp(OUTPUTS.forall { (x: Box) =>
    !(x.tokens.exists { (token: (Coll[Byte], Long)) => token._1 == SELF.tokens(0)._1 }) ||
    (
      x.R7[GroupElement].get == SELF.R7[GroupElement].get &&
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

class Token(TypedDict):
    token_id: str
    amount: int

class Registers(TypedDict):
    r4: Optional[str]
    r5: Optional[str]
    r6: Optional[str]
    r7: Optional[str]
    r8: Optional[str]

# Utility function to convert InputBox to dict
def __input_box_to_dict(input_box: 'org.ergoplatform.appkit.InputBoxImpl') -> dict:
    return json.loads(str(input_box.toJson(True)))

# Function to build the proof box
def __build_proof_box(
    ergo: appkit.ErgoAppKit,
    input_boxes: java.util.ArrayList,
    proof_id: str,
    sender_address: Address,
    token_amount: int = DEFAULT_TOKEN_AMOUNT,
    reputation_token_label: str = DEFAULT_TOKEN_LABEL,
    assigned_object: Optional[ProofObject] = None
):
    object_type_to_assign = assigned_object['type'] if assigned_object else ProofObjectType.PlainText
    object_to_assign = assigned_object['value'] if assigned_object else ""

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([ErgoToken(proof_id, jpype.JLong(abs(token_amount)))]) \
                .registers([
                    ErgoValue.of(jpype.JString(reputation_token_label).getBytes("utf-8")),         # R4
                    ErgoValue.of(jpype.JString(object_type_to_assign.value).getBytes("utf-8")),    # R5
                    ErgoValue.of(jpype.JString(object_to_assign).getBytes("utf-8")),               # R6
                    ErgoValue.of(sender_address.toPropositionBytes()),                             # R7   TODO: https://discord.com/channels/668903786361651200/849659724495323206/1278352612680400948
                    ErgoValue.of(jpype.JBoolean(token_amount >= 0))                                # R8
                ]) \
                .contract(ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)) \
                .build()

@initialize_jvm
def __create_reputation_proof_tx(node_url: str, wallet_mnemonic: str, proof_id: str, objects: List[Tuple[str, int]]):
    ergo = appkit.ErgoAppKit(node_url=node_url)
    fee = DEFAULT_FEE  # Fee in nanoErgs

    # 1. Get the change address
    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=None)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

    LOGGER(f"Sender address -> {sender_address.toString()}")

    # 2. Prepare transaction inputs (get UTXOs)
    wallet_input_boxes = ergo.getInputBoxCovering(amount_list=[fee], sender_address=sender_address)

    # Select the input box with min value to avoid NotEnoughErgsError
    selected_input_box = min(
        (input_box for input_box in wallet_input_boxes if __input_box_to_dict(input_box)["value"] > 2 * SAFE_MIN_BOX_VALUE),
        key=lambda ib: __input_box_to_dict(ib)["value"],
        default=None
    )

    if not selected_input_box:
        raise Exception("No input box available.")

    total_token_value = sum([obj[1] for obj in objects])  # Should be the TOTAL_REPUTATION_TOKEN_AMOUNT.
    assert TOTAL_REPUTATION_TOKEN_AMOUNT == total_token_value, (
        "The sum of the values to be spent must equal the total reputation token amount."
    )
    LOGGER(f"Needs to be spent {total_token_value} reputation value.")
    input_boxes = [selected_input_box]
    if proof_id:
        try:
            # TODO should get all the boxes with CONTRACT and this token.
            input_boxes.extend(ergo.getInputBoxCovering(amount_list=[],
                sender_address=ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT),  # ??
                tokenList=[proof_id], amount_tokens=[total_token_value])
            )
        except Exception as e:
            LOGGER(f"Exception submitting with the last proof_id {str(e)}. A new one will be generated.")
            proof_id = None

    LOGGER(f"Input boxes -> {[__input_box_to_dict(_i) for _i in input_boxes]}")

    java_input_boxes = java.util.ArrayList(input_boxes)

    LOGGER(f"selected_input_box value: {__input_box_to_dict(selected_input_box)['value']}")
    LOGGER(f"fee: {fee}, SAFE_MIN_BOX_VALUE: {SAFE_MIN_BOX_VALUE}")
    value_in_ergs = (__input_box_to_dict(selected_input_box)["value"] - fee - SAFE_MIN_BOX_VALUE) / 10**9

    # 3. Build transaction outputs
    outputs = []

    # Check reputation proof id.
    proof_id = proof_id if proof_id else java_input_boxes.get(0).getId().toString()  # Assume that, if it is not an empty string, the proof ID corresponds to an existing token ID.

    LOGGER(f"proof id -> {proof_id}")

    # Reputation proof output box
    for obj in objects:
        proof_box = __build_proof_box(
            ergo=ergo,
            input_boxes=java_input_boxes,
            proof_id=proof_id,
            sender_address=sender_address,
            assigned_object=ProofObject(
                type=ProofObjectType.PlainText,
                value=obj[0]
            ),
            token_amount=obj[1]
        )
        if proof_box:
            outputs.append(proof_box)

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

    if REVIEWER_REPUTATION_PROOF_ID != proof_id:
        LOGGER(f"Store reviewer reputation proof id {proof_id} on .env file.")
        write_env("REVIEWER_REPUTATION_PROOF_ID", proof_id)
        if not  REVIEWER_REPUTATION_PROOF_ID == proof_id:
            LOGGER(f"Proof ID was not stored correctly: {REVIEWER_REPUTATION_PROOF_ID} != {proof_id}")

    return signed_tx if True else tx_id

def submit_reputation_proof(objects: List[Tuple[str, int]]) -> bool:
    try:
        tx_id = __create_reputation_proof_tx(
            node_url=ERGO_NODE_URL,
            wallet_mnemonic=ERGO_WALLET_MNEMONIC,
            proof_id=REVIEWER_REPUTATION_PROOF_ID,
            objects=objects,
        )
        LOGGER(f"Submited tx -> {tx_id}")
        return tx_id != None
    except Exception as e:
        LOGGER(str(e))
        return False
