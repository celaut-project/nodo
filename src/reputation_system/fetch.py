from typing import Generator

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_script, set_token_id


env_manager = ConfigManager()

def local_proofs() -> Generator[celaut.Contract, None, None]:
    """The reputation proofs this node announces, each attested to its owner.

    A proof travels with the signature of the wallet that published it, over this
    node's ``peer_id`` -- without it a reader can see an owner in R7 and has no way to
    tie it to the peer announcing the proof, since the two are different keys (the
    identity is on no ledger; see ``node_identity``). Attaching it here, where the proof
    is built, keeps every announced proof attested by construction.
    """
    from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE, ergo_ledger
    from src.reputation_system.proof_attestation import attest_proof_ownership

    proof_id = env_manager.get('ledgers.ergo.reputation.REPUTATION_PROOF_ID')
    if proof_id:
        contract = celaut.Contract(ledger=ergo_ledger)
        set_script(contract, bytes.fromhex(REPUTATION_PROOF_ERGO_TREE))
        set_token_id(contract, proof_id)
        # Best-effort: an unattested proof is announced anyway and simply not credited
        # by a reader, which is better than announcing no proof at all.
        attest_proof_ownership(
            contract, str(env_manager.get('ledgers.ergo.WALLET_MNEMONIC', '') or '')
        )
        yield contract
    
def get_reputation_proofs_by_hash() -> Generator[celaut.Contract, None, None]:
    pass  # TODO
