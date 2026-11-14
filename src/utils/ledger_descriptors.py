"""The ``Contract.Ledger`` of every ledger celaut uses, written down completely.

A ledger is declared the way every replaceable component in celaut is -- ``tags``,
``prose``, ``formal`` -- and, like a protocol layer (``src/identity/transport_stack.py``),
it has to state everything two nodes must have agreed on to use it together, and nothing
that each one chooses for itself. A wallet's derivation path, a payee's deposit-token
lifetime or the supply of a reputation proof are each node's own choice and are not here.

One kind of own choice *is* here, because a payer cannot pay correctly without it: a
**payee parameter** (``PAYEE_PARAMETERS``). It is a value the payee selects and states
for the payer -- today ``payment.min_confirmations`` and ``payment.too_early`` on
Bitcoin (#626). They are not part of what two nodes must agree on, so a comparison of two
payment declarations (``same_payment_ledger``) ignores them: two nodes that wait for a
different number of confirmations, and a node from before these lines, still settle on
the same ledger.

A descriptor has two parts:

* **The network** -- which chain and which network, and how its assets and units are
  named. The definition of the ledger itself, and repeated whole wherever that ledger
  appears: in ``Peer.payment_contracts`` and again in ``Peer.reputation_proofs``. Each
  place has to be readable on its own; saving the bytes is a concern for whoever
  transports or stores the announcement, not for what it says.
* **The use** -- what celaut does on it at that place, and nothing more: a payment
  contract says how a payment is bound to a deposit token and what its attributes hold;
  a reputation proof says how its boxes are laid out and how its owner attests a peer.
  A payment contract carries no reputation rules, and a proof no payment rules.

**The tag names the chain and the network**: ``bitcoin-mainnet``, ``bitcoin-regtest``,
``ergo-mainnet``, ``ergo-<16 hex of the genesis block>``. One interface module (Bitcoin,
Ergo) settles on several networks, and each network is a different ledger. The tag is
the identity of the ledger in the database and on the wire, so a row or a key written
on one network is never read as one of another. The *chain* (``bitcoin``, ``ergo``,
``chain_of``) stays the name of the config block (``ledgers.<chain>``) and of the
command line.

``formal`` is canonical ``key=value`` lines (``node_identity.component_formal``) and is
what a comparison reads (``node_identity.same_component``). ``prose`` is the same in
ASD-STE100 Simplified Technical English, for a reader.
"""
from typing import Dict, Optional, Tuple

from protos import celaut_pb2
from src.identity.node_identity import component_formal
from src.utils.config import ConfigManager

# Chain names: the config block (`ledgers.<chain>`) and the first part of each tag.
ERGO = "ergo"
BITCOIN = "bitcoin"

# The genesis block of Ergo mainnet. Also in `reputation_system.contracts.ergo.utils`,
# which needs a JVM to import; this module must not.
ERGO_MAINNET_GENESIS_BLOCK_ID = (
    "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b"
)

_CELAUT_NODE_TYPE_NFT_KEY = "ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID"
_PLAIN_TEXT_TYPE_NFT_KEY = "ledgers.ergo.reputation.PLAIN_TEXT_TYPE_NFT_ID"

Part = Tuple[Dict[str, str], str]

# The confirmations a Bitcoin payee needs before it accepts a payment (#626).
MIN_CONFIRMATIONS_KEY = "payment.min_confirmations"
# What a payee does with a `Payable` that comes before that number (#626).
TOO_EARLY_KEY = "payment.too_early"
_BITCOIN_MIN_CONFIRMATIONS_CONFIG = "ledgers.bitcoin.payments.MIN_CONFIRMATIONS"

# Keys of a payment declaration that the payee selects for itself and states for the
# payer. They are not part of the ledger's identity: `same_payment_ledger` ignores them.
PAYEE_PARAMETERS = frozenset({MIN_CONFIRMATIONS_KEY, TOO_EARLY_KEY})

# The largest ``payment.min_confirmations`` a payer accepts from a payee. A Bitcoin block
# comes about every 10 minutes and a deposit token lives 6 hours
# (``contracts.bitcoin.interface.DEPOSIT_TOKEN_TTL``), so a payee that wants more than 36
# confirmations wants more than the token usually lives for: the payer would pay and the
# token would expire before the payer can tell the payee. The same limit as chatui.
MAX_PAYEE_MIN_CONFIRMATIONS = 36


def _ledger(tag: str, *parts: Part) -> celaut_pb2.Contract.Ledger:
    pairs: Dict[str, str] = {}
    for part_pairs, _ in parts:
        pairs.update(part_pairs)
    prose = "\n\n".join(part_prose for _, part_prose in parts)
    return celaut_pb2.Contract.Ledger(tags=[tag], prose=prose, formal=component_formal(pairs))


# ---------------------------------------------------------------------------------
# Ergo
# ---------------------------------------------------------------------------------

def _ergo_network_name() -> str:
    """Which Ergo network this node is on, from ``ledgers.ergo.GENESIS_BLOCK_ID``.

    The genesis block is what the config already uses to say which chain this node
    belongs on (``manager.ergo`` refuses an Ergo node that reports another one). Only
    mainnet's id is known to the code, so any other chain is named by its genesis id
    itself: two nodes on the same testnet or private chain declare the same network,
    and neither declares mainnet.
    """
    genesis = _ergo_genesis()
    if genesis == ERGO_MAINNET_GENESIS_BLOCK_ID:
        return "mainnet"
    return f"genesis {genesis or 'unset'}"


def _ergo_genesis() -> str:
    return str(ConfigManager().get("ledgers.ergo.GENESIS_BLOCK_ID") or "").strip().lower()


def ergo_tag() -> str:
    """``ergo-mainnet``, or ``ergo-`` and the first 16 hex characters of the genesis block.

    16 hex characters (64 bits) are enough to tell two Ergo chains apart, and keep the
    tag short. A node with no genesis block set gives ``ergo-unset``.
    """
    genesis = _ergo_genesis()
    if genesis == ERGO_MAINNET_GENESIS_BLOCK_ID:
        return f"{ERGO}-mainnet"
    return f"{ERGO}-{genesis[:16] or 'unset'}"


def _ergo_network() -> Part:
    network = _ergo_network_name()
    return {
        "chain": "ergo",
        "network": network,
        "consensus": "autolykos2",
        "model": "eutxo",
        "script": "ErgoTree",
        "asset.native": "ERG",
        "asset.native.base_unit": "nanoERG",
        "asset.native.decimals": "9",
        "asset.token": "EIP-4 token id, 64 hex characters, compared without case",
    }, (
        f"This ledger is Ergo {network}. Ergo is a proof-of-work blockchain (Autolykos) "
        "with an extended UTXO model. ErgoTree scripts lock the boxes. The native asset "
        "is ERG. One ERG is 10^9 nanoERG. A token is an EIP-4 token. Its id is 64 "
        "hexadecimal characters. Compare ids without case."
    )


def _ergo_payment() -> Part:
    return {
        "payment.rate.unit": "ContractRate.mu_per_unit is MU per base unit of the "
                             "asset: one nanoERG, or one base unit of a token",
        "payment.xattr.script": "ErgoTree bytes (propositionBytes) that receive the payment",
        "payment.xattr.address": "informative; payment.xattr.script decides",
        "payment.xattr.token_id": "ERG, or the EIP-4 token id of the asset",
        "payment.xattr.contract_type": "UTF-8 name of the kind of contract",
        "payment.contract_type.p2pk": "proveDlog(decodePoint())",
        "payment.output": "one box to payment.xattr.script with at least the amount: "
                          "nanoERG for ERG, base units for a token",
        "payment.deposit_token": "R4 of that box: Coll[Byte], UTF-8 of the deposit token",
        "payment.accepted": "that box exists and is unspent, in a block or in the mempool",
    }, (
        "PAYMENT. ContractRate.mu_per_unit gives the MU for one base unit of the asset: "
        "one nanoERG, or the smallest unit of a token. The script attribute is the ErgoTree that "
        "receives the payment. The token_id attribute is \"ERG\" or the id of the token. "
        "The contract_type attribute is the kind of contract, as UTF-8 text. "
        "\"proveDlog(decodePoint())\" is a pay-to-public-key contract. The address "
        "attribute is only for a person. The script attribute decides.\n"
        "To pay, make one box that the script attribute locks. The box must contain the "
        "amount or more: nanoERG for ERG, base units for a token. Put the deposit token "
        "in register R4, as Coll[Byte] of its UTF-8 bytes. The payee accepts the payment "
        "when this box exists and is not spent. The box can be in a block or in the "
        "mempool."
    )


def _ergo_reputation() -> Part:
    from src.identity.node_identity import attestation_payload
    from src.reputation_system.envs import (
        DIGITAL_PUBLIC_GOOD_SCRIPT_HASH,
        REPUTATION_PROOF_ERGO_TREE,
    )

    config = ConfigManager()
    attestation_prefix = attestation_payload("")
    return {
        "reputation.contract.ergo_tree": REPUTATION_PROOF_ERGO_TREE,
        "reputation.contract.dpg_script_hash": DIGITAL_PUBLIC_GOOD_SCRIPT_HASH,
        "reputation.xattr.script": "reputation.contract.ergo_tree",
        "reputation.xattr.token_id": "the token id of the proof",
        "reputation.box.token": "the proof token; its amount is the weight of the opinion",
        "reputation.R4": "Coll[Byte]: type NFT id, raw bytes",
        # Which ids mark a celaut node's proof is a network-wide agreement the operator
        # states: a node configured with another id reads and writes boxes no other
        # node recognises, and its descriptor says so.
        "reputation.R4.celaut_node": str(config.get(_CELAUT_NODE_TYPE_NFT_KEY, "") or ""),
        "reputation.R4.plain_text": str(config.get(_PLAIN_TEXT_TYPE_NFT_KEY, "") or ""),
        "reputation.R5": "Coll[Byte]: the object; raw bytes if it is hex, else UTF-8; "
                         "for a celaut node, its peer id; the proof token id marks a "
                         "reserve box",
        "reputation.R6": "Boolean: is locked",
        "reputation.R7": "Coll[Byte]: owner propositionBytes, 0008cd || 33-byte "
                         "compressed public key",
        "reputation.R8": "Boolean: true for a positive opinion",
        "reputation.R9": "Coll[Byte]: UTF-8 content; on the box about the node itself, "
                         "its signed Peer in protobuf JSON",
        "reputation.attestation.payload": f"{attestation_prefix}<peer_id>",
        "reputation.attestation.xattrs": "owner_public_key, owner_signature",
        "reputation.attestation.message": "UTF-8 of the payload, not hashed first",
        "reputation.attestation.public_key": "secp256k1, 33-byte SEC compressed, "
                                             "lowercase hex; R7 is 0008cd || this key",
        "reputation.attestation.signature": "65 bytes, lowercase hex: a (33-byte "
                                            "compressed point) || z (32-byte big-endian "
                                            "scalar)",
        "reputation.attestation.challenge": "e = blake2b256(a || message || public_key), "
                                            "read as a signed big-endian integer",
        "reputation.attestation.verify": "z*G == a + e*P on secp256k1",
    }, (
        "REPUTATION PROOFS. A reputation proof is a token. The boxes of a proof use the "
        "ErgoTree that the formal field gives. The token amount in a box is the weight "
        "of one opinion. The registers are:\n"
        "R4: the type NFT id, as raw bytes. The formal field gives the id for a celaut "
        "node and the id for plain text.\n"
        "R5: the object of the opinion. If it is hexadecimal, use its raw bytes. If not, "
        "use UTF-8. For a celaut node, it is the peer id. If R5 is the proof token id, "
        "the box is a reserve box.\n"
        "R6: a Boolean. True if the box is locked.\n"
        "R7: the propositionBytes of the owner: 0008cd, then the 33-byte compressed "
        "public key.\n"
        "R8: a Boolean. True for a positive opinion.\n"
        "R9: UTF-8 content. In the box about the node itself, it is the signed Peer of "
        "the node, in protobuf JSON.\n"
        "OWNER ATTESTATION. The owner of a proof signs the text "
        f"\"{attestation_prefix}<peer_id>\". The proof carries the result in its "
        "attributes owner_public_key and owner_signature. The public key is a 33-byte "
        "compressed secp256k1 key. The signature is 65 bytes: a point a (33 bytes, "
        "compressed), then a scalar z (32 bytes, big-endian). To verify:\n"
        "1. Calculate e as the BLAKE2b-256 hash of a, then the message in UTF-8, then "
        "the public key. Read e as a signed big-endian integer.\n"
        "2. Make sure that z*G is equal to a + e*P.\n"
        "3. Make sure that R7 of the proof box is 0008cd followed by the public key."
    )


def ergo_payment_ledger() -> celaut_pb2.Contract.Ledger:
    """Ergo, as a payment contract carries it: the network and how a payment is made."""
    return _ledger(ergo_tag(), _ergo_network(), _ergo_payment())


def ergo_reputation_ledger() -> celaut_pb2.Contract.Ledger:
    """Ergo, as a reputation proof carries it: the network and how a proof is laid out."""
    return _ledger(ergo_tag(), _ergo_network(), _ergo_reputation())


# ---------------------------------------------------------------------------------
# Bitcoin
# ---------------------------------------------------------------------------------

def _bitcoin_network_name() -> str:
    """``ledgers.bitcoin.NETWORK``: mainnet, testnet, signet or regtest.

    Read with the same default as ``payment_system.contracts.bitcoin.interface.NETWORK``,
    which is what this node derives its own addresses and scripts with. A scriptPubKey
    is the same bytes on every network, so this line is the only thing that keeps a
    mainnet payer from paying a signet node real BTC it will never see.
    """
    return str(ConfigManager().get("ledgers.bitcoin.NETWORK") or "mainnet").strip().lower()


def bitcoin_tag() -> str:
    """``bitcoin-`` and the network: ``bitcoin-mainnet``, ``bitcoin-regtest``, ..."""
    return f"{BITCOIN}-{_bitcoin_network_name()}"


def _bitcoin_network() -> Part:
    network = _bitcoin_network_name()
    return {
        "chain": "bitcoin",
        "network": network,
        "consensus": "sha256d-pow",
        "model": "utxo",
        "asset.native": "BTC",
        "asset.native.base_unit": "satoshi",
        "asset.native.decimals": "8",
    }, (
        f"This ledger is Bitcoin {network}. Bitcoin is a proof-of-work blockchain with a "
        "UTXO model. The only asset is BTC. One BTC is 10^8 satoshi."
    )


def bitcoin_min_confirmations() -> int:
    """``ledgers.bitcoin.payments.MIN_CONFIRMATIONS``: 1 or more, 1 when it is not set.

    The one place this value is read. The Bitcoin contract validates with it
    (``contracts.bitcoin.interface.MIN_CONFIRMATIONS``) and the declaration states it, so
    the two cannot be different.
    """
    try:
        value = int(ConfigManager().get(_BITCOIN_MIN_CONFIRMATIONS_CONFIG, 1) or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _bitcoin_payment() -> Part:
    min_confirmations = bitcoin_min_confirmations()
    return {
        "payment.rate.unit": "ContractRate.mu_per_unit is MU per satoshi",
        "payment.xattr.script": "scriptPubKey bytes that receive the payment",
        "payment.xattr.address": "informative; payment.xattr.script decides",
        "payment.xattr.token_id": "BTC",
        "payment.xattr.contract_type": "UTF-8 name of the kind of contract",
        "payment.contract_type.p2wpkh": "p2wpkh",
        "payment.output": "one output to payment.xattr.script with at least the amount, "
                          "in satoshi",
        "payment.deposit_token": "an OP_RETURN output of the same transaction; its data "
                                 "is the UTF-8 deposit token, at most 80 bytes",
        # Unchanged from before #626, so a node with this declaration and one from
        # before it still match: the number and the too-early rule are payee
        # parameters below, and `same_payment_ledger` does not compare them.
        "payment.accepted": "the transaction is confirmed; the payee selects how many "
                            "confirmations",
        MIN_CONFIRMATIONS_KEY: str(min_confirmations),
        TOO_EARLY_KEY: "if the transaction pays the amount and has fewer confirmations, "
                       "the payee keeps the deposit token pending and answers "
                       "\"not confirmed yet\"; the payer calls Payable again before "
                       "the token expires",
    }, (
        "PAYMENT. ContractRate.mu_per_unit gives the MU for one satoshi. The script "
        "attribute is the scriptPubKey that receives the payment. The token_id attribute "
        "is \"BTC\". The contract_type attribute is the kind of contract, as UTF-8 text. "
        "\"p2wpkh\" is pay-to-witness-public-key-hash. The address attribute is only for "
        "a person. The script attribute decides.\n"
        "To pay, make one transaction. It must have one output to the script attribute "
        "with the amount or more, in satoshi. It must also have one OP_RETURN output. The "
        "data of the OP_RETURN output is the deposit token in UTF-8, 80 bytes or less. "
        "The payee accepts the payment when the transaction has "
        f"{min_confirmations} confirmations or more (payment.min_confirmations). The "
        "payee selects this number. Wait for this number of confirmations, then call "
        "Payable. If you call Payable too early, the payee does not refuse the deposit "
        "token. It keeps the token pending and gives the error \"not confirmed yet\". "
        "Call Payable again before the token expires."
    )


def bitcoin_payment_ledger() -> celaut_pb2.Contract.Ledger:
    """Bitcoin, as a payment contract carries it."""
    return _ledger(bitcoin_tag(), _bitcoin_network(), _bitcoin_payment())


# ---------------------------------------------------------------------------------
# Payee parameters
# ---------------------------------------------------------------------------------

def _formal_lines(formal: bytes):
    return bytes(formal).split(b"\n")


def _without_payee_parameters(formal: bytes) -> bytes:
    """``formal`` less its payee-parameter lines, every other byte as it was."""
    return b"\n".join(
        line for line in _formal_lines(formal)
        if line.split(b"=", 1)[0].decode("utf-8", "replace") not in PAYEE_PARAMETERS
    )


def same_payment_ledger(a, b) -> bool:
    """Whether two payment declarations name the same ledger.

    ``node_identity.same_declaration`` (a non-empty ``formal`` on both sides, equal byte
    for byte), but without the payee-parameter lines: a payee states them for the payer,
    and two nodes that select different values are still on the same ledger. Each other
    line stays compared byte for byte, so this is as strict as before for everything two
    nodes must agree on.
    """
    formal_a, formal_b = bytes(a.formal), bytes(b.formal)
    if not formal_a or not formal_b:
        return False
    return _without_payee_parameters(formal_a) == _without_payee_parameters(formal_b)


def payee_parameter(ledger, key: str) -> Optional[str]:
    """The value of the payee parameter ``key`` in ``ledger``, or None if it is not there."""
    if ledger is None:
        return None
    prefix = f"{key}=".encode("utf-8")
    for line in _formal_lines(getattr(ledger, "formal", b"") or b""):
        if line.startswith(prefix):
            return line[len(prefix):].decode("utf-8", "replace")
    return None


def declared_min_confirmations(ledger) -> Optional[int]:
    """``payment.min_confirmations`` of a payee's declaration, or None if it gives none.

    Raises ValueError for a value that is not an integer from 1 to
    ``MAX_PAYEE_MIN_CONFIRMATIONS``. Such a payee cannot be paid correctly, and the
    payer must not guess a number for it.
    """
    value = payee_parameter(ledger, MIN_CONFIRMATIONS_KEY)
    if value is None:
        return None
    try:
        number = int(value.strip())
    except ValueError:
        raise ValueError(f"{MIN_CONFIRMATIONS_KEY}={value!r} is not an integer")
    if not 1 <= number <= MAX_PAYEE_MIN_CONFIRMATIONS:
        raise ValueError(
            f"{MIN_CONFIRMATIONS_KEY}={number} is not from 1 to "
            f"{MAX_PAYEE_MIN_CONFIRMATIONS}"
        )
    return number


def payee_parameters_reason(ledger) -> Optional[str]:
    """Why the payee parameters of ``ledger`` cannot be used, or None when they can."""
    try:
        declared_min_confirmations(ledger)
    except ValueError as e:
        return str(e)
    return None


# ---------------------------------------------------------------------------------
# By tag
# ---------------------------------------------------------------------------------

_PAYMENT_LEDGERS = {ERGO: ergo_payment_ledger, BITCOIN: bitcoin_payment_ledger}
_REPUTATION_LEDGERS = {ERGO: ergo_reputation_ledger}
_TAGS = {ERGO: ergo_tag, BITCOIN: bitcoin_tag}


def chain_of(tag: str) -> str:
    """The chain of a ledger tag: ``bitcoin-regtest`` -> ``bitcoin``.

    The name of the config block (``ledgers.<chain>``). A chain name gives itself, and a
    tag of no known chain (``simulated``) gives itself too.
    """
    tag = str(tag or "")
    chain = tag.split("-", 1)[0]
    return chain if chain in _TAGS else tag


def ledger_tag(chain: str) -> str:
    """The tag of the network this node is configured on for ``chain``.

    ``chain`` may also be a tag: the tag of its chain *now* is given back. A name that is
    not a known chain (``simulated``) is given back as it is.
    """
    tag_of = _TAGS.get(chain_of(chain))
    return tag_of() if tag_of else str(chain or "")


def _by_tag(builders, tag: str) -> Optional[celaut_pb2.Contract.Ledger]:
    """This node's declaration on ``tag``, or None when ``tag`` is not its network now.

    A tag of the same chain on another network (``bitcoin-mainnet`` on a regtest node)
    gives None: this node does not settle there.
    """
    build = builders.get(chain_of(tag))
    if build is None:
        return None
    ledger = build()
    return ledger if list(ledger.tags) == [tag] else None


def payment_ledger(tag: str) -> Optional[celaut_pb2.Contract.Ledger]:
    """This node's declaration of ``tag`` for a payment contract, or None."""
    return _by_tag(_PAYMENT_LEDGERS, tag)


def reputation_ledger(tag: str) -> Optional[celaut_pb2.Contract.Ledger]:
    """This node's declaration of ``tag`` for a reputation proof, or None."""
    return _by_tag(_REPUTATION_LEDGERS, tag)


def payment_ledgers():
    return [build() for build in _PAYMENT_LEDGERS.values()]


def reputation_ledgers():
    return [build() for build in _REPUTATION_LEDGERS.values()]
