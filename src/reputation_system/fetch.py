from typing import Generator

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_script, set_token_id


env_manager = ConfigManager()

def local_proofs() -> Generator[celaut.Contract, None, None]:
    from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE, ergo_ledger

    proof_id = env_manager.get('ledgers.ergo.reputation.REPUTATION_PROOF_ID')
    if proof_id:
        contract = celaut.Contract(ledger=ergo_ledger)
        set_script(contract, bytes.fromhex(REPUTATION_PROOF_ERGO_TREE))
        set_token_id(contract, proof_id)
        yield contract
    
def get_reputation_proofs_by_hash() -> Generator[celaut.Contract, None, None]:
    pass  # TODO
