import hashlib
from pathlib import Path
from typing import Optional

from protos import celaut_pb2
from src.utils.config import ConfigManager


LEDGER = "ergo"

_CONTRACTS_DIR = Path("src/reputation_system/contracts/ergo")
_DIGITAL_PUBLIC_GOOD_PATH = _CONTRACTS_DIR / "digital_public_good.es"
_REPUTATION_PROOF_PATH = _CONTRACTS_DIR / "reputation_proof.es"
_DGP_HASH_PLACEHOLDER = "`+DIGITAL_PUBLIC_GOOD_SCRIPT_HASH+`"


def _read_contract(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _java_bytes_to_python_bytes(java_bytes) -> bytes:
    return bytes((byte + 256) % 256 for byte in java_bytes)


def _compile_script_hash(script: str, node_url: str) -> Optional[str]:
    try:
        from ergpy import appkit
        from org.ergoplatform.appkit import ConstantsBuilder
        from jpype import JPackage

        ergo = appkit.ErgoAppKit(node_url=node_url)
        contract = ergo._ctx.compileContract(ConstantsBuilder.empty(), script)
        ergo_tree = contract.getErgoTree()
        serializer = JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
        serialized = serializer.serializeErgoTree(ergo_tree)
        return hashlib.blake2b(_java_bytes_to_python_bytes(serialized), digest_size=32).hexdigest()
    except Exception:
        return None


DIGITAL_PUBLIC_GOOD_CONTRACT = _read_contract(_DIGITAL_PUBLIC_GOOD_PATH)

# blake2b256 of the *compiled* Digital-Public-Good ErgoTree bytes (ErgoTree v1), i.e.
# the exact value the reputation-systems ecosystem (web app, Game of Prompts, skills,
# forum, …) substitutes into reputation_proof.es. Hashing the .es *source text*, or
# hashing a different ErgoTree version, yields a different contract and a different
# on-chain address the ecosystem cannot see. Kept as a constant so importing this module
# stays JVM-free; `_compile_script_hash` re-derives the same value when a node has a JVM.
DIGITAL_PUBLIC_GOOD_SCRIPT_HASH = "ceea52651b6b206381ea28a2e59f775367cef567c0c2f089dc7e09356b64ef61"

REPUTATION_PROOF_TEMPLATE = _read_contract(_REPUTATION_PROOF_PATH)
CONTRACT = REPUTATION_PROOF_TEMPLATE.replace(_DGP_HASH_PLACEHOLDER, DIGITAL_PUBLIC_GOOD_SCRIPT_HASH)

# Canonical reputation-proof P2S address: reputation_proof.es compiled as ErgoTree v1
# with DIGITAL_PUBLIC_GOOD_SCRIPT_HASH above — the single contract instance the whole
# ecosystem publishes to and reads from. nodo MUST mint and scan proofs at THIS address.
# AppKit's compileContract() emits ErgoTree v0, which lands the box at a different
# address the web app / Game of Prompts / skills / forum cannot read, so we target this
# fixed address instead of recompiling locally.
REPUTATION_PROOF_ADDRESS = "6axptaZbz6n5h3MUjsWMf4ptPTFjqF6BKoyqsrN4VNTs1impDs2DbLzcL12u8mPqDSm6sauaabcnyVoaxW7fcqqZdMisMRFwTgGcgkQrCWdsoRmvCGGynHMtMb6Ygp51BT6WF2GkezH95xzBCmpYPuTcoSBqbSacHhSJuUaTLLfR5j5q4Gnfeej7UFDJdvrXURpg6pF2EJjYNwFumeNobppRGuAbnVchWcVrPTRwuGZKjNwUaPwKZnDSA7mxm5kKBro5JEXCS2o9LaPr5kks26Qt3fdmmci3V44dwbE8FBnuMA3vUHzxHBia1bsLR862ykfAddtzP8XAt2NMa7aaJpXf5cMyK4gFqAdwnHLjsTeZnR6zQHL2UvNWujmaooZMZ26tvV4YvPQnbynACNVe5tUpRfFyrPb4WFKk6C4oby7R9aELDD2MpiZ2Yq4picCvUXPiCw6Dvd4JbGxayVSTNsa3c7EF88NvFuKbfmkCGGrozhqBokg1zmc2iGSi5ucjA2yWJ5F29gJWuEwUXrm1qrzw9ZxUbpZm37AJswhU9g1dnHSaXHSniQPxdrwysn838A8tS2KyCFfk3tAfsD2eQDjhYpRLMVEuJXGy7QMnnUMvbMF6zNCwASuRAGVRLyyYY6MLzrus1VxcWFS3vi7zjTMBoTgWdKRZTw5VJ5QrxEQgXDXB8kD94RWh5dmgAXBfGDCFKZoFVM7W1c6xqsfwduFWQApnvqQXedapoqCxs7qERarqfPa6ykRPkJpzrcXppof2YiG8d7oVnZndJgHTixkGUaSKrXmF6V9mvqJ48kLfjzBMbjyPeohk6e6v8Td1YQbFjWL89ocDUfdDtxCTQ67NiaU3T53fwivHukYrSz4EVah7PLjjC9PvZdgGs5CQoMBNqpHMz5WaefUcGEovZf9G9t2Lm96pjufto58DGPu8M2TvRjUwLqkCwc8VdkpHhcyuvrFNUoQtfX7m3oMJVvcWhrVujtMpiyByATK9939XFuZUe92fkpbyTKhEcGdeJhDXjgJgvKi6jMEpvgcHRtrqm5kykb19iHWnDeCH5T78GZpShYWCuFJpTEFSdRzptUfguXQiRVz8p38H71PMtG8sxYyCDTLrBD8Lf4cSELUKxscVj8Z4Pxjmsg1v88DS2Z3H4avf81iNGMme7eJfjtsHScH3jX623WKy3N6wVgvHpqzcdjGR"

PROSE = "Ergo system: PoW blockchain using Autolykos with verifiable eUTXO model, non-Turing-complete Sigma scripts, finite emission with linear reduction, on-chain miner-signaled governance, and cryptographic security via Merkle trees, proof-of-work, and zero-knowledge proofs."

ergo_ledger = celaut_pb2.Contract.Ledger(
    tags=[LEDGER],
    prose=PROSE,
    formal="".encode("utf-8"),
)
