from binascii import hexlify
from typing import List
import requests

from src.utils.config import ConfigManager
from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module
from src.utils.logger import LOGGER

def get_public_key(mnemonic_phrase: str) -> object:
    """
    Obtains the public key in hexadecimal format from the mnemonic phrase.

    :param mnemonic_phrase: BIP-39 mnemonic phrase.
    :return: Public key in org.ergoplatform.appkit.Address | tip: use address.toString() to obtain the hexadecimal string.
    """
    ergpy = require_java_module("ergpy.appkit", feature="Ergo reputation")
    ergo = ergpy.ErgoAppKit(node_url=ConfigManager().get("ledgers.ergo.NODE_URL"))
    mnemonic = ergo.getMnemonic(wallet_mnemonic=mnemonic_phrase, mnemonic_password=None)
    return ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

"""
@initialize_jvm
def pub_key_hex_to_addr(pub_key_hex: str) -> str:
    
    publicKeyBytes = bytes.fromhex(pub_key_hex)
    
    publicKey = GroupElement.fromBytes(publicKeyBytes);
    
    proveDlog = ProveDlog.apply(publicKey);
    
    address = Address.fromErgoTree(proveDlog.ergoTree(), NetworkType.MAINNET);
    
    return address
"""

def addr_to_pub_key_hex(address: str) -> str:
    ensure_ergpy_jvm(feature="Ergo reputation")
    jpype = require_java_module("jpype", feature="Ergo reputation")
    org_ergoplatform = jpype.JPackage("org").ergoplatform

    pk = address.getPublicKey()
    ec_point = pk.value()
    group_element = org_ergoplatform.JavaHelpers.SigmaDsl().GroupElement(ec_point)
    java_bytes = group_element.getEncoded()  # sigma.data.CollOverArray$mcB$sp
    java_byte_array = java_bytes.toArray()
    python_bytes = bytes([(byte + 256) % 256 for byte in java_byte_array])
    public_key_hex = hexlify(python_bytes).decode('utf-8')
    return public_key_hex


def get_boxes_by_token_ids(ergo, node_url: str, token_ids: List[str]) -> list:
    """
    Fetch boxes by token IDs using the node URL (for resolving IDs) and the ErgoAppKit context.
    """
    if not node_url:
        raise ValueError("Missing configuration: ledgers.ergo.NODE_URL")

    unique_ids = {token_id for token_id in token_ids if token_id}
    if not unique_ids:
        return []

    box_ids = []
    for token_id in unique_ids:
        url = f"{node_url}/blockchain/box/byTokenId/{token_id}"
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            raise ValueError(f"Could not fetch token {token_id}: HTTP {response.status_code}")

        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            raise ValueError(f"Token {token_id} was not found in explorer response")

        for item in items:
            box_ids.append(item["boxId"])

    if not box_ids:
        return []

    jpype = require_java_module("jpype", feature="Ergo reputation")
    ctx = ergo._ctx

    try:
        jarray_cls = jpype.JArray(jpype.JString)
        java_box_ids = jarray_cls(box_ids)
        boxes = ctx.getBoxesById(java_box_ids)
        return list(boxes)
    except Exception as e:
        LOGGER(f"BlockchainContext.getBoxesById failed: {e}")
        raise RuntimeError(f"Failed to fetch boxes by ID via BlockchainContext: {e}")

