import hashlib
from binascii import hexlify
from typing import List, Optional, Tuple
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


def owner_script_hash_hex(address) -> str:
    """
    blake2b256(propositionBytes) of an address' ErgoTree, as hex. This is the value a
    Reputation Box stores in R7 to identify its owner. Single source of truth reused by
    the reputation transaction builder and the proof-ownership lookup.
    """
    jpype = require_java_module("jpype", feature="Ergo reputation")
    ergo_tree = address.getErgoAddress().script()
    serializer = jpype.JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
    proposition_bytes = bytes((byte + 256) % 256 for byte in serializer.serializeErgoTree(ergo_tree))
    return hashlib.blake2b(proposition_bytes, digest_size=32).hexdigest()


def compile_contract_template(ergo, script: str) -> Tuple[str, str]:
    """
    Compile an ErgoScript contract and return (mainnet P2S address, ergoTree template hash).
    The template hash is what the Explorer's box-search endpoint filters on.
    """
    jpype = require_java_module("jpype", feature="Ergo reputation")
    org_appkit = jpype.JPackage("org").ergoplatform.appkit
    ergo_tree = ergo._ctx.compileContract(org_appkit.ConstantsBuilder.empty(), script).getErgoTree()

    template = ergo_tree.template()
    template_array = template.toArray() if hasattr(template, "toArray") else template
    template_bytes = bytes((byte + 256) % 256 for byte in template_array)
    template_hash = hashlib.blake2b(template_bytes, digest_size=32).hexdigest()

    address = str(org_appkit.Address.fromErgoTree(ergo_tree, org_appkit.NetworkType.MAINNET).toString())
    return address, template_hash


def search_unspent_boxes(ergo, template_hash: str, registers: Optional[dict] = None, limit: int = 20) -> List[dict]:
    """
    Look up unspent boxes for a contract (by ErgoTree template hash) and, crucially, filter
    by register values *server-side* via the Explorer `POST /api/v1/boxes/unspent/search`
    endpoint — so only the matching boxes are returned instead of every box at the address.
    """
    api_url = str(ergo.get_api_url()).rstrip("/")
    body: dict = {"ergoTreeTemplateHash": template_hash}
    if registers:
        body["registers"] = registers

    url = f"{api_url}/api/v1/boxes/unspent/search?limit={limit}&offset=0"
    response = requests.post(url, json=body, timeout=30)
    if response.status_code != 200:
        raise ValueError(f"Box search failed: HTTP {response.status_code} - {response.text[:200]}")

    payload = response.json()
    return payload.get("items", []) if isinstance(payload, dict) else (payload or [])

