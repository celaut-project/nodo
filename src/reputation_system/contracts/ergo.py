import json
from ergpy import appkit
from ergpy.helper_functions import initialize_jvm
import jpype
from enum import Enum
from typing import List, TypedDict, Optional

from utils.logger import LOGGER
from utils.env import ERGO_NODE_URL, ERGO_WALLET_MNEMONIC

from jpype import *
import java.lang

from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *

# Constants
DEFAULT_FEE = 1_000_000
SAFE_MIN_BOX_VALUE = 1_000_000
DEFAULT_TOKEN_AMOUNT = 1_000_000_000
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
    sender_address: Address,
    token_amount: int = DEFAULT_TOKEN_AMOUNT,
    reputation_token_label: str = DEFAULT_TOKEN_LABEL,
    assigned_object: Optional[ProofObject] = None,
    polarization: bool = True
):
    object_type_to_assign = assigned_object['type'] if assigned_object else ProofObjectType.PlainText
    object_to_assign = assigned_object['value'] if assigned_object else ""

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([ErgoToken(input_boxes.get(0).getId().toString(), jpype.JLong(token_amount))]) \
                .registers([
                    ErgoValue.of(jpype.JString(reputation_token_label).getBytes("utf-8")),         # R4
                    ErgoValue.of(jpype.JString(object_type_to_assign.value).getBytes("utf-8")),    # R5
                    ErgoValue.of(jpype.JString(object_to_assign).getBytes("utf-8")),               # R6
                    ErgoValue.of(sender_address.toPropositionBytes()),                             # R7
                    ErgoValue.of(jpype.JBoolean(polarization))                                     # R8
                ]) \
                .contract(ergo._ctx.compileContract(ConstantsBuilder.empty(), CONTRACT)) \
                .build()

@initialize_jvm
def __create_reputation_proof_tx(node_url: str, wallet_mnemonic: str, assigned_object: Optional[ProofObject], polarization: bool = True):
    ergo = appkit.ErgoAppKit(node_url=node_url)
    fee = DEFAULT_FEE  # Fee in nanoErgs

    # 1. Get the change address
    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=None)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

    print(f"Sender address -> {sender_address.toString()}")

    # 2. Prepare transaction inputs (get UTXOs)
    input_boxes = ergo.getInputBoxCovering(amount_list=[fee], sender_address=sender_address)

    # Select the input box with min value to avoid NotEnoughErgsError
    selected_input_box = min(
        (input_box for input_box in input_boxes if __input_box_to_dict(input_box)["value"] > 2 * SAFE_MIN_BOX_VALUE),
        key=lambda ib: __input_box_to_dict(ib)["value"],
        default=None
    )

    if not selected_input_box:
        raise Exception("No input box available.")

    selected_input_box_obj = __input_box_to_dict(selected_input_box)
    selected_input_boxes = java.util.ArrayList([selected_input_box])

    value_in_ergs = (selected_input_box_obj["value"] - fee - SAFE_MIN_BOX_VALUE) / 10**9

    # 3. Build transaction outputs
    outputs = []

    # Reputation proof output box
    proof_box = __build_proof_box(
        ergo=ergo,
        input_boxes=selected_input_boxes,
        sender_address=sender_address,
        assigned_object=assigned_object,
        polarization=polarization
    )
    if proof_box:
        outputs.append(proof_box)

    # Basic wallet output box
    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[sender_address.toString()], amount_list=[value_in_ergs])
    if output_boxes[0]:
        outputs.extend(output_boxes)

    # 4. Build and sign the transaction
    unsigned_tx = ergo.buildUnsignedTransaction(
        input_box=selected_input_boxes,
        outBox=outputs,
        fee=fee / 10**9,
        sender_address=sender_address
    )
    signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index=0)

    # 5. Submit the transaction and return the ID
    tx_id = ergo.txId(signed_tx)
    print(f"Transaction ID: {tx_id}", flush=True)

    return signed_tx if True else tx_id

def submit_reputation_proof(object: str, polarization: bool) -> bool:
    tx_id = __create_reputation_proof_tx(
        node_url=ERGO_NODE_URL,
        wallet_mnemonic=ERGO_WALLET_MNEMONIC,
        assigned_object=object,
        polarization=polarization
    )
    LOGGER(f"Submited tx -> {tx_id}")
    return tx_id != None
