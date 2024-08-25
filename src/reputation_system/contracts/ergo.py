import json
from ergpy import appkit
import jpype
from typing import List, TypedDict, Optional

from jpype import *
import java.lang

from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *


SAFE_MIN_BOX_VALUE = 1_000_000

# Initialize JVM before calling the decorated function
def initialize_jvm(function):
    def wrapper(*args, **kwargs):
        try:
            jpype.addClassPath('ergo.jar')
            jpype.startJVM()
        except OSError:
            pass
        finally:
            res = function(*args, **kwargs)
            return res
    return wrapper

class Token(TypedDict):
    token_id: str
    amount: int

class Registers(TypedDict):
    r4: Optional[str]
    r5: Optional[str]
    r6: Optional[str]
    r7: Optional[str]
    r8: Optional[str]

def input_box_to_dict(input_box: 'org.ergoplatform.appkit.InputBoxImpl') -> dict:
    return json.loads(str(input_box.toJson(True)))

def build_proof_box(
    ergo: appkit.ErgoAppKit,
    input_boxes: java.util.ArrayList,  # java.util.ArrayList[InputBox]
    sender_address: str
):
    # This function currently returns None, as the logic isn't defined yet.

    token_amount = 1_000_000
    reputation_token_label = "REPUTATION_PROOF"
    object_type_to_assign = "plain-txt"
    object_to_assign = ".empty"
    owner_address = sender_address # generate_pk_proposition
    polarization = True

    contract_address = sender_address # TODO don't use the sender address. Use the reputation proof contract address.
    print(f"Contract address: {contract_address}", flush=True)

    return ergo._ctx.newTxBuilder() \
            .outBoxBuilder() \
                .value(SAFE_MIN_BOX_VALUE) \
                .tokens([
                    ErgoToken(
                        input_boxes.get(0).getId().toString(),
                        jpype.JLong(token_amount)
                    )
                ]) \
                .registers([  #  Strings should be SConstant(SColl(SByte, stringToBytes('utf-8', Value)))
                    ErgoValue.of(jpype.JString(reputation_token_label).getBytes("utf-8")),   # R4
                    ErgoValue.of(jpype.JString(object_type_to_assign).getBytes("utf-8")),    # R5
                    ErgoValue.of(jpype.JString(object_to_assign).getBytes("utf-8")),         # R6
                    ErgoValue.of(jpype.JString(owner_address).getBytes("utf-8")),            # R7
                    ErgoValue.of(jpype.JBoolean(polarization))                               # R8
                ]) \
                .contract(
                    ErgoTreeContract(
                        Address.create(contract_address).getErgoAddress().script(),
                        ergo._networkType
                    )
                ) \
            .build()

@initialize_jvm
def create_reputation_proof_tx(ergo: appkit.ErgoAppKit, wallet_mnemonic: str):
    mnemonic_password: Optional[str] = None
    prover_index: int = 0
    return_signed: bool = True
    fee: int = 1_000_000  # Fee in nanoErgs

    # 1. Get the change address
    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=mnemonic_password)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

    # 2. Prepare transaction inputs (get UTXOs)
    input_boxes = ergo.getInputBoxCovering(amount_list=[fee], sender_address=sender_address)
    print(f"Input boxes: {input_boxes}", flush=True)

    # 3. Build transaction outputs
    outputs = []

    #
    # TODO: Make better code.
    #
    # Get the input box with min value to avoid NotEnoughErgsError.
    _input_box, _input_box_obj = None, None
    java.util.ArrayList([])
    for _ib in input_boxes:
        _ib_obj = input_box_to_dict(_ib)
        if _ib_obj["value"] > 2*SAFE_MIN_BOX_VALUE:
            if not _input_box_obj or _input_box_obj and _input_box_obj["value"] >= _ib_obj["value"]:
                _input_boxes = java.util.ArrayList([_ib])
                _input_box, _input_box_obj = _ib, _ib_obj

    print(f"Selected input box {_input_box_obj['boxId']}", flush=True)
    value_in_ergs = (_input_box_obj["value"] - fee - SAFE_MIN_BOX_VALUE) / 10**9  # Convert from nanoErgs to Ergs
    print(f"Requested value in Ergs: {value_in_ergs}", flush=True)

    # Reputation proof output box.
    proof_box = build_proof_box(ergo, input_boxes=_input_boxes, sender_address=sender_address.toString())
    print(f"proof box -> {proof_box}", flush=True)
    if proof_box:
        outputs.append(proof_box)

    # Basic wallet output box.
    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[sender_address.toString()], amount_list=[value_in_ergs])
    if not output_boxes[0]:
        print("Output box is null.", flush=True)
        return None

    outputs.extend(output_boxes)
    print(f"Outputs: {outputs}", flush=True)
    print(f"Output values: {[output.getValue() for output in outputs]}", flush=True)

    # 4. Build and sign the transaction
    unsigned_tx = ergo.buildUnsignedTransaction(
        input_box=_input_boxes,
        outBox=outputs,
        fee=fee / 10**9,
        sender_address=sender_address
    )
    print(f"Unsigned transaction: {unsigned_tx}", flush=True)

    signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index)
    print(f"Signed transaction: {signed_tx}", flush=True)

    # 5. Submit the transaction and return the ID
    tx_id = ergo.txId(signed_tx)
    print(f"Transaction ID: {tx_id}", flush=True)

    return signed_tx if return_signed else tx_id

if __name__ == "__main__":
    wallet_mnemonic = "decline reward asthma enter three clean borrow repeat identify wisdom horn pull entire adapt neglect"
    node_url: str = "http://213.239.193.208:9052/"  # MainNet or TestNet
    ergo = appkit.ErgoAppKit(node_url=node_url)

    tx_id = create_reputation_proof_tx(ergo=ergo, wallet_mnemonic=wallet_mnemonic)
    print(f"Transaction: {tx_id}", flush=True)
