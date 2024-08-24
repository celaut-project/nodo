import json
from ergpy import appkit
import jpype
from typing import List, TypedDict, Optional

from jpype import *
import java.lang


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

def build_proof_box(ergo: appkit.ErgoAppKit, value: int, address: str, tokens: List[Token], registers: Registers):
    # This function currently returns None, as the logic isn't defined yet.
    return None

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
    contract_address = sender_address.toString()
    print(f"Contract address: {contract_address}", flush=True)

    # Get the input box with min value to avoid NotEnoughErgsError.
    _input_box = min(input_boxes, key=lambda box: input_box_to_dict(box)['value'], default=input_boxes[0])  # bad practice (two times converted to dict)
    input_box = input_box_to_dict(_input_box)
    print(f"Selected input box {input_box['boxId']}", flush=True)
    value_in_ergs = (input_box["value"] - fee) / 10**9  # Convert from nanoErgs to Ergs
    print(f"Requested value in Ergs: {value_in_ergs}", flush=True)

    # Currently not adding proof box, as the buildProofBox function returns None
    # proof_box = build_proof_box(ergo, value=value_in_ergs, address=contract_address, tokens=[], registers={})
    output_boxes = ergo.buildOutBox(receiver_wallet_addresses=[contract_address], amount_list=[value_in_ergs])

    if not output_boxes[0]:
        print("Output box is null.", flush=True)
        return None

    outputs.extend(output_boxes)
    print(f"Outputs: {outputs}", flush=True)
    print(f"Output values: {[output.getValue() for output in output_boxes]}", flush=True)

    # 4. Build and sign the transaction
    unsigned_tx = ergo.buildUnsignedTransaction(
        input_box=java.util.ArrayList([_input_box]),
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
