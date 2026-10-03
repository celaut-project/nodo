"""The ``Contract.Ledger`` of every ledger celaut uses, written down completely.

A ledger is declared the way every replaceable component in celaut is -- ``tags``,
``prose``, ``formal`` -- and, like a protocol layer (``src/identity/transport_stack.py``),
it has to state everything two nodes must have agreed on to use it together, and nothing
that each one chooses for itself. A wallet's derivation path, a payee's confirmation
count, its deposit-token lifetime or the supply of a reputation proof are each node's
own choice and are not here.

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

``formal`` is canonical ``key=value`` lines (``node_identity.component_formal``) and is
what a comparison reads (``node_identity.same_component``). ``prose`` is the same in
ASD-STE100 Simplified Technical English, for a reader.
"""
from typing import Dict, Optional, Tuple

from protos import celaut_pb2
from src.identity.node_identity import component_formal
from src.utils.config import ConfigManager

ERGO = "ergo"
BITCOIN = "bitcoin"

_CELAUT_NODE_TYPE_NFT_KEY = "ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID"
_PLAIN_TEXT_TYPE_NFT_KEY = "ledgers.ergo.reputation.PLAIN_TEXT_TYPE_NFT_ID"

Part = Tuple[Dict[str, str], str]


def _ledger(tag: str, *parts: Part) -> celaut_pb2.Contract.Ledger:
    pairs: Dict[str, str] = {}
    for part_pairs, _ in parts:
        pairs.update(part_pairs)
    prose = "\n\n".join(part_prose for _, part_prose in parts)
    return celaut_pb2.Contract.Ledger(tags=[tag], prose=prose, formal=component_formal(pairs))


# ---------------------------------------------------------------------------------
# Ergo
# ---------------------------------------------------------------------------------

def _ergo_network() -> Part:
    return {
        "chain": "ergo",
        "network": "mainnet",
        "consensus": "autolykos2",
        "model": "eutxo",
        "script": "ErgoTree",
        "asset.native": "ERG",
        "asset.native.base_unit": "nanoERG",
        "asset.native.decimals": "9",
        "asset.token": "EIP-4 token id, 64 hex characters, compared without case",
    }, (
        "This ledger is Ergo mainnet. Ergo is a proof-of-work blockchain (Autolykos) "
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
    return _ledger(ERGO, _ergo_network(), _ergo_payment())


def ergo_reputation_ledger() -> celaut_pb2.Contract.Ledger:
    """Ergo, as a reputation proof carries it: the network and how a proof is laid out."""
    return _ledger(ERGO, _ergo_network(), _ergo_reputation())


# ---------------------------------------------------------------------------------
# Bitcoin
# ---------------------------------------------------------------------------------

def _bitcoin_network() -> Part:
    return {
        "chain": "bitcoin",
        "network": "mainnet",
        "consensus": "sha256d-pow",
        "model": "utxo",
        "asset.native": "BTC",
        "asset.native.base_unit": "satoshi",
        "asset.native.decimals": "8",
    }, (
        "This ledger is Bitcoin mainnet. Bitcoin is a proof-of-work blockchain with a "
        "UTXO model. The only asset is BTC. One BTC is 10^8 satoshi."
    )


def _bitcoin_payment() -> Part:
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
        "payment.accepted": "the transaction is confirmed; the payee selects how many "
                            "confirmations",
    }, (
        "PAYMENT. ContractRate.mu_per_unit gives the MU for one satoshi. The script "
        "attribute is the scriptPubKey that receives the payment. The token_id attribute "
        "is \"BTC\". The contract_type attribute is the kind of contract, as UTF-8 text. "
        "\"p2wpkh\" is pay-to-witness-public-key-hash. The address attribute is only for "
        "a person. The script attribute decides.\n"
        "To pay, make one transaction. It must have one output to the script attribute "
        "with the amount or more, in satoshi. It must also have one OP_RETURN output. The "
        "data of the OP_RETURN output is the deposit token in UTF-8, 80 bytes or less. "
        "The payee accepts the payment when the transaction is confirmed. The payee "
        "selects how many confirmations it needs."
    )


def bitcoin_payment_ledger() -> celaut_pb2.Contract.Ledger:
    """Bitcoin, as a payment contract carries it."""
    return _ledger(BITCOIN, _bitcoin_network(), _bitcoin_payment())


# ---------------------------------------------------------------------------------
# By tag
# ---------------------------------------------------------------------------------

_PAYMENT_LEDGERS = {ERGO: ergo_payment_ledger, BITCOIN: bitcoin_payment_ledger}
_REPUTATION_LEDGERS = {ERGO: ergo_reputation_ledger}


def payment_ledger(tag: str) -> Optional[celaut_pb2.Contract.Ledger]:
    """This node's declaration of ``tag`` for a payment contract, or None."""
    build = _PAYMENT_LEDGERS.get(tag)
    return build() if build else None


def reputation_ledger(tag: str) -> Optional[celaut_pb2.Contract.Ledger]:
    """This node's declaration of ``tag`` for a reputation proof, or None."""
    build = _REPUTATION_LEDGERS.get(tag)
    return build() if build else None


def payment_ledgers():
    return [build() for build in _PAYMENT_LEDGERS.values()]


def reputation_ledgers():
    return [build() for build in _REPUTATION_LEDGERS.values()]
