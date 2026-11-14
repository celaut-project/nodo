from binascii import hexlify
from typing import Iterator, List, NamedTuple, Optional
import requests
from src.routing import ledger_http

from src.utils.config import ConfigManager
from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module
from src.utils.logger import LOGGER

# The explorer AppKit resolves per network type, hard-coded there as
# `RestApiErgoClient.getDefaultExplorerUrl`. Repeated here so a read that needs
# nothing but HTTP does not have to start a JVM to learn a constant.
MAINNET_EXPLORER = "https://api.ergoplatform.com"
TESTNET_EXPLORER = "https://api-testnet.ergoplatform.com"

# Ergo mainnet's genesis block. `ledgers.ergo.GENESIS_BLOCK_ID` is what the config
# already uses to say which chain this node belongs on -- `manager.ergo` refuses an
# Ergo node whose `/info` reports a different one -- so the two together are what
# names the network here.
from src.utils.ledger_descriptors import ERGO_MAINNET_GENESIS_BLOCK_ID as MAINNET_GENESIS_BLOCK_ID


def explorer_api_url() -> str:
    """The explorer this node reads.

    The same value ``ErgoAppKit.get_api_url()`` resolves, without a JVM: a network has
    one default explorer, and the configured genesis block says which network this is.
    Callers that already hold an ``ergo`` handle should keep using it -- this exists for
    the read-only paths that would otherwise pay a JVM start, and a JPype JVM cannot be
    started twice in a process anyway.

    Read from the config rather than by asking the Ergo node, which is the same
    question it would answer but makes an explorer read depend on the node being up.
    They are separate services; one being unreachable must not take the other with it,
    and reputation is read entirely off the explorer.
    """
    genesis = str(ConfigManager().get("ledgers.ergo.GENESIS_BLOCK_ID") or "").strip().lower()
    if genesis == MAINNET_GENESIS_BLOCK_ID:
        return MAINNET_EXPLORER
    # Anything else is not mainnet, and the testnet explorer is the only other one
    # AppKit knows. A private chain has no public explorer either way, so there is no
    # third answer to give -- and a wrong guess here reads as "no opinions", which is
    # why `manager.ergo` refuses a node that disagrees with this key in the first place.
    LOGGER(
        f"ledgers.ergo.GENESIS_BLOCK_ID is not mainnet's ({genesis or 'unset'}); "
        "reading reputation from the testnet explorer."
    )
    return TESTNET_EXPLORER


# Serialized-constant value lengths, by sigma type code, for
# :func:`ergo_tree_template`. Only what a contract in this tree actually uses; anything
# else raises rather than guessing a length and hashing the wrong bytes.
_FIXED_VALUE_BYTES = {
    0x01: 1,   # SBoolean
    0x02: 1,   # SByte
    0x07: 33,  # SGroupElement
}
_VLQ_VALUE_TYPES = (0x03, 0x04, 0x05)  # SShort, SInt, SLong: zigzag VLQ
_COLL_BYTE = 0x0E  # Coll[SByte]: VLQ length, then that many bytes


def _read_vlq(data: bytes, index: int) -> tuple:
    """One VLQ-encoded integer at ``index``, as ``(value, index after it)``."""
    value = 0
    shift = 0
    while True:
        if index >= len(data):
            raise ValueError("Truncated VLQ in ErgoTree.")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, index
        shift += 7


def ergo_tree_template(tree_hex: str) -> bytes:
    """The *template* of an ErgoTree: its root expression, constants stripped.

    This is what the explorer's box search is keyed by
    (``ergoTreeTemplateHash``), so deriving it is what lets a reputation query be a
    register-filtered request instead of a scan of the whole contract.

    A v1 tree with constant segregation lays out as: header byte, VLQ total size,
    VLQ constant count, the serialized constants, then the root. Skipping the
    constants means knowing how many bytes each occupies, which is why the type codes
    above are enumerated -- an unknown one raises, because a template hashed from the
    wrong offset is a valid-looking hash that matches nothing.

    Note that a template is deliberately *less* specific than a tree: two contracts
    with the same code and different constants share it. Callers must still check the
    ErgoTree itself (see :func:`opinions_about`'s client-side filter).
    """
    data = bytes.fromhex(tree_hex)
    if not data:
        raise ValueError("Empty ErgoTree.")

    header = data[0]
    index = 1
    if header & 0x08:  # the size flag: a VLQ byte count of everything after it
        _, index = _read_vlq(data, index)
    if not header & 0x10:  # no constant segregation: the root starts here
        return data[index:]

    count, index = _read_vlq(data, index)
    for constant in range(count):
        if index >= len(data):
            raise ValueError("Truncated constant section in ErgoTree.")
        type_code = data[index]
        index += 1
        if type_code in _FIXED_VALUE_BYTES:
            index += _FIXED_VALUE_BYTES[type_code]
        elif type_code in _VLQ_VALUE_TYPES:
            _, index = _read_vlq(data, index)
        elif type_code == _COLL_BYTE:
            length, index = _read_vlq(data, index)
            index += length
        else:
            raise ValueError(
                f"Constant {constant} of the ErgoTree has type {hex(type_code)}, whose "
                "serialized length this reader does not know."
            )
    return data[index:]


def ergo_tree_template_hash(tree_hex: str) -> str:
    """``sha256`` of :func:`ergo_tree_template`, hex — the explorer's search key.

    Derived rather than pinned so it cannot drift from the ErgoTree it belongs to;
    ``tests/test_node_reputation.py`` pins the value for the reputation contract, which
    was checked against the live explorer.
    """
    from hashlib import sha256

    return sha256(ergo_tree_template(tree_hex)).hexdigest()


def iter_unspent_boxes_by_registers(
    api_url: str,
    template_hash: str,
    registers: dict,
    page_size: int = 100,
    max_boxes: int = 2000,
) -> Iterator[dict]:
    """
    Yield unspent boxes on one contract template whose registers match ``registers``,
    via the Explorer `POST /api/v1/boxes/unspent/search`, paginated.

    ``registers`` maps a register name to the **rendered** value -- the raw payload hex,
    with no ``0e``/length prefix. That is not a style choice: the endpoint matches
    rendered values and silently returns nothing for a serialized one, so a filter
    written the other way reads as "no such box" rather than as an error. Build a value
    with :func:`decode_coll_byte_hex` if all you hold is the serialized form.

    ``template_hash`` is required by the endpoint; passing nothing there is rejected
    with HTTP 400. Note it identifies the contract's *code*, not the exact tree, so
    callers must still check ``ergoTree`` on what comes back.
    """
    api_url = str(api_url).rstrip("/")
    body = {
        "ergoTreeTemplateHash": template_hash,
        "registers": dict(registers),
        "assets": [],
    }
    offset = 0
    fetched = 0
    while fetched < max_boxes:
        url = f"{api_url}/api/v1/boxes/unspent/search?offset={offset}&limit={page_size}"
        response = ledger_http.post(url, json=body, timeout=30)
        if response.status_code != 200:
            raise ValueError(
                f"Register-filtered box search failed: HTTP {response.status_code} "
                f"- {response.text[:200]}"
            )

        items = response.json().get("items", [])
        if not items:
            break
        for box in items:
            yield box
            fetched += 1
        if len(items) < page_size:
            break
        offset += len(items)

    if fetched >= max_boxes:
        LOGGER(f"Reached the {max_boxes}-box cap while searching {registers}.")


def iter_unspent_boxes_by_token(
    api_url: str, token_id: str, page_size: int = 100, max_boxes: int = 2000,
    deadline: Optional[float] = None,
) -> Iterator[dict]:
    """
    Yield the unspent boxes holding ``token_id``, via the Explorer
    `GET /api/v1/boxes/unspent/byTokenId/{token_id}`, paginated.

    For a reputation proof that is every box it currently holds, at any address. The
    caller selects the boxes at the contract (see :func:`proof_standing`).

    ``deadline`` is a ``time.monotonic()`` value. No request starts after it, and no
    request waits past it (or past 30 s).
    """
    import time

    api_url = str(api_url).rstrip("/")
    offset = 0
    fetched = 0
    while fetched < max_boxes:
        timeout = 30.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Out of time while reading the boxes of token {token_id}.")
            timeout = min(timeout, remaining)
        url = (
            f"{api_url}/api/v1/boxes/unspent/byTokenId/{token_id}"
            f"?limit={page_size}&offset={offset}"
        )
        response = ledger_http.get(url, timeout=timeout)
        if response.status_code != 200:
            raise ValueError(
                f"Box lookup for token {token_id} failed: HTTP {response.status_code}"
            )

        items = response.json().get("items", [])
        if not items:
            break
        for box in items:
            yield box
            fetched += 1
        if len(items) < page_size:
            break
        offset += len(items)

    if fetched >= max_boxes:
        LOGGER(f"Reached the {max_boxes}-box cap while totalling token {token_id}.")


class ProofStanding(NamedTuple):
    """What a reputation proof has put where, read from its own boxes in one pass.

    Only the boxes at the reputation contract that hold the proof's token count. A box
    elsewhere (a wallet) can be spent by its owner at any time, so its ERG is not locked
    and its tokens are not opinions.
    """

    #: Tokens the proof has assigned to opinions: everything it holds at the contract
    #: *except* what sits in self-opinions. See :func:`proof_standing`.
    assigned_amount: int
    #: Tokens in self-opinions: boxes whose R5 is the proof's own token id.
    reserved_amount: int
    #: The ERG locked behind the opinions of the proof, in nanoERG:
    #: ``contract_nanoerg * assigned_amount / (assigned_amount + reserved_amount)``.
    burned_nanoerg: int
    #: All the nanoERG in the proof's boxes at the contract, before that share.
    contract_nanoerg: int = 0


def proof_standing(
    api_url: str, token_id: str, deadline: Optional[float] = None,
) -> ProofStanding:
    """What the reputation proof ``token_id`` has assigned, reserved and locked.

    **Only the boxes at the reputation contract** (``ergoTree`` is
    ``REPUTATION_PROOF_ERGO_TREE``) whose first asset is the proof count. The explorer
    gives every box that holds the token, at any address. A box in a wallet is not
    locked: its owner can spend it, and one wallet box that holds one unit each of N
    tokens would otherwise back all N proofs with the same ERG.

    **Assigned** is the denominator a stake on this proof is a share of. A proof keeps
    its unassigned tokens in a self-opinion: a box whose R5 is its own token id -- the
    ecosystem's self-pointing "profile" box, which ``profileFetch``'s
    ``is_self_defined`` filter selects on. That reserve is not a judgement about
    anything. On mainnet it is nearly the whole supply of every live profile.

    **Locked** (``burned_nanoerg``) is the ERG behind the opinions of the proof::

        SUM(ERG in the proof's boxes at the contract)
            * tokens not in a self-opinion / all the proof's tokens at the contract

    The ERG can be in the box of any opinion, or in any other box of the proof at the
    contract. The contract's owner path requires ``totalNativeOut >= totalNativeIn``
    across the proof's boxes, so the owner cannot take it back out, and the public
    top-up path lets anybody add to it. A proof that keeps nearly all its tokens in a
    self-opinion puts nearly none of its ERG behind its opinions.

    ``deadline`` is a ``time.monotonic()`` value for all the requests together.
    """
    from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE

    tree = REPUTATION_PROOF_ERGO_TREE.lower()
    token = token_id.lower()
    assigned = 0
    reserved = 0
    contract = 0
    for box in iter_unspent_boxes_by_token(api_url, token_id, deadline=deadline):
        if str(box.get("ergoTree") or "").lower() != tree:
            continue
        assets = box.get("assets") or []
        if not assets or str(assets[0].get("tokenId") or "").lower() != token:
            continue
        contract += int(box.get("value") or 0)
        amount = int(assets[0].get("amount") or 0)
        pointer = (decode_coll_byte_hex(str(box_register(box, "R5") or "")) or "").lower()
        if pointer == token:
            reserved += amount
        else:
            assigned += amount
    total = assigned + reserved
    locked = contract * assigned // total if total > 0 else 0
    return ProofStanding(
        assigned_amount=assigned,
        reserved_amount=reserved,
        burned_nanoerg=locked,
        contract_nanoerg=contract,
    )


def decode_coll_byte_hex(register_value: str) -> Optional[str]:
    """
    Return the raw byte payload (hex) of a Coll[Byte] register.

    Handles the node's serialized form -- ``0e`` (Coll[Byte] type tag) + VLQ length +
    payload -- and the explorer's already-rendered raw-hex form. No fixed-length
    assumption: R7 holds the owner ``propositionBytes`` (~35 bytes for a P2PK), R4/R5
    hold 32-byte token ids, etc.
    """
    if not register_value:
        return None

    value = register_value.strip().lower()
    if not value:
        return None

    if value.startswith("0e"):
        # Serialized Coll[Byte]: 0e <VLQ length> <payload bytes>.
        idx = 2
        length = 0
        shift = 0
        try:
            while idx + 1 < len(value):
                byte = int(value[idx:idx + 2], 16)
                idx += 2
                length |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
        except ValueError:
            return None
        payload = value[idx:idx + length * 2]
        return payload or None

    # Already the rendered raw-hex payload.
    return value


def decode_bool_register(register_value: str) -> Optional[bool]:
    """A Boolean register's value, in whichever form the source rendered it.

    Two forms reach this. Serialized -- what the Ergo node returns and what the
    explorer puts in ``serializedValue`` -- is the ``01`` SBoolean type code followed
    by the byte, so ``0101`` is true and ``0100`` false. Rendered is the plain word.

    ``None`` when it is neither, which is a box that does not follow the canonical
    layout. That is deliberately not the same answer as ``False``: R8 is the polarity
    of an opinion, and reading "no declared polarity" as "against" would invent a
    hostile stake nobody published.
    """
    value = (register_value or "").strip().lower()
    if value in ("true", "false"):
        return value == "true"
    if value in ("0101", "0100"):
        return value.endswith("01")
    return None


def box_register(box: dict, register: str) -> Optional[str]:
    """One register of a box, whichever shape the source renders it in.

    The Ergo node hands back a bare serialized string; the explorer hands back an
    object carrying both the serialized and the rendered form. Both reach
    :func:`decode_coll_byte_hex`, which is why either is acceptable here.
    """
    additional = box.get("additionalRegisters", {})
    reg = additional.get(register)

    if reg is None:
        return None

    if isinstance(reg, str):
        return reg

    if isinstance(reg, dict):
        return reg.get("serializedValue") or reg.get("renderedValue")

    return None

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
        response = ledger_http.get(url, timeout=15)
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


def owner_proposition_bytes(address) -> bytes:
    """
    Raw ``propositionBytes`` (serialized ErgoTree) of an address' script.

    This is the value a Reputation Box stores in R7 to identify its owner. The
    reputation_proof.es contract authorises the admin/spend path with
    ``INPUTS.exists { b.propositionBytes == SELF.R7[Coll[Byte]].get }`` — so R7 must
    hold the *raw* propositionBytes, NOT a hash, or the owner could never spend the
    box (and the reputation-system web app, Game of Prompts, skills, forum, … all
    read R7 as the raw propositionBytes too). Single source of truth reused by the
    reputation transaction builder and the proof-ownership lookup.
    """
    jpype = require_java_module("jpype", feature="Ergo reputation")
    ergo_tree = address.getErgoAddress().script()
    serializer = jpype.JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
    return bytes((byte + 256) % 256 for byte in serializer.serializeErgoTree(ergo_tree))


def owner_proposition_bytes_hex(address) -> str:
    """Hex of :func:`owner_proposition_bytes` — the R7 owner value as stored/compared."""
    return owner_proposition_bytes(address).hex()


def get_contract_address(ergo, script: str) -> str:
    """Compile an ErgoScript contract and return its mainnet P2S address."""
    jpype = require_java_module("jpype", feature="Ergo reputation")
    org_appkit = jpype.JPackage("org").ergoplatform.appkit
    ergo_tree = ergo._ctx.compileContract(org_appkit.ConstantsBuilder.empty(), script).getErgoTree()
    return str(org_appkit.Address.fromErgoTree(ergo_tree, org_appkit.NetworkType.MAINNET).toString())


def iter_unspent_boxes_by_address(api_url: str, address: str, page_size: int = 50, max_boxes: int = 2000) -> Iterator[dict]:
    """
    Yield unspent boxes at a single contract address via the Explorer
    `GET /api/v1/boxes/unspent/byAddress/{address}`, paginated (same access pattern as
    payment_system.payment_process_validator).

    This is scoped to one contract, not the whole chain; callers filter client-side and
    should break as soon as they find what they need so the common case fetches one page.

    Takes the explorer URL rather than an ``ergo`` handle: the paging is plain HTTP, and
    a caller with no other use for AppKit should not have to start a JVM to reach it
    (see :func:`explorer_api_url`). A caller that already holds one passes
    ``ergo.get_api_url()``.
    """
    api_url = str(api_url).rstrip("/")
    offset = 0
    fetched = 0
    while fetched < max_boxes:
        url = f"{api_url}/api/v1/boxes/unspent/byAddress/{address}?limit={page_size}&offset={offset}"
        response = ledger_http.get(url, timeout=30)
        if response.status_code != 200:
            raise ValueError(f"Box lookup failed: HTTP {response.status_code} - {response.text[:200]}")

        items = response.json().get("items", [])
        if not items:
            break
        for box in items:
            yield box
            fetched += 1
        if len(items) < page_size:
            break
        offset += page_size

    if fetched >= max_boxes:
        LOGGER(f"Reached the {max_boxes}-box cap while scanning {address} for a reputation proof.")

