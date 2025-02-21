from typing import Generator

from protos import celaut_pb2 as celaut
from src.reputation_system.envs import CONTRACT, LEDGER
from src.utils.env import EnvManager


env_manager = EnvManager()

def local_proofs() -> Generator[celaut.ContractLedger, None, None]:
    yield celaut.ContractLedger(
        contract=CONTRACT.encode("utf-8"),
        contract_addr=env_manager.get_env('REPUTATION_PROOF_ID'),
        ledger=LEDGER
    )
    
def get_reputation_proofs_by_hash() -> Generator[celaut.ContractLedger, None, None]:
    pass  # TODO