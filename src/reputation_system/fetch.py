from typing import Generator

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_script, set_token_id


env_manager = ConfigManager()

def local_proofs() -> Generator[celaut.Contract, None, None]:
    from src.reputation_system.envs import CONTRACT, ergo_ledger

    proof_id = env_manager.get('REPUTATION_PROOF_ID')
    if proof_id:
        contract = celaut.Contract(ledger=ergo_ledger)
        set_script(contract, CONTRACT.encode("utf-8"))
        set_token_id(contract, proof_id)
        yield contract
    
def get_reputation_proofs_by_hash() -> Generator[celaut.Contract, None, None]:
    pass  # TODO
