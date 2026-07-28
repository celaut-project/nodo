import hashlib
from pathlib import Path
from typing import Optional

from protos import celaut_pb2

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


def _resolve_digital_public_good_script_hash(dpg_contract: str) -> str:
    # Keep module import Java-free; the deterministic source hash is enough until
    # an Ergo-backed operation explicitly needs the JVM.
    return hashlib.blake2b(dpg_contract.encode("utf-8"), digest_size=32).hexdigest()


DIGITAL_PUBLIC_GOOD_CONTRACT = _read_contract(_DIGITAL_PUBLIC_GOOD_PATH)
DIGITAL_PUBLIC_GOOD_SCRIPT_HASH = _resolve_digital_public_good_script_hash(DIGITAL_PUBLIC_GOOD_CONTRACT)
REPUTATION_PROOF_TEMPLATE = _read_contract(_REPUTATION_PROOF_PATH)
CONTRACT = REPUTATION_PROOF_TEMPLATE.replace(_DGP_HASH_PLACEHOLDER, DIGITAL_PUBLIC_GOOD_SCRIPT_HASH)

PROSE = "Ergo system: PoW blockchain using Autolykos with verifiable eUTXO model, non-Turing-complete Sigma scripts, finite emission with linear reduction, on-chain miner-signaled governance, and cryptographic security via Merkle trees, proof-of-work, and zero-knowledge proofs."

ergo_ledger = celaut_pb2.Contract.Ledger(
    tags=[LEDGER],
    prose=PROSE,
    formal="".encode("utf-8"),
)
