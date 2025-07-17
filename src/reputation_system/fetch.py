from typing import Generator

from protos import celaut_pb2 as celaut
from src.reputation_system.envs import CONTRACT, ergo_ledger
from src.utils.env import EnvManager


env_manager = EnvManager()

def local_proofs() -> Generator[celaut.Contract, None, None]:
    proof_id = env_manager.get_env('REPUTATION_PROOF_ID')
    if proof_id:
        yield celaut.Contract(
            template=celaut.Contract.ScriptTemplate(
                prose="",
                formal=CONTRACT.encode("utf-8")
            ),
            script=b"",
            token_id=proof_id,
            ledger=ergo_ledger
        )
    
def get_reputation_proofs_by_hash() -> Generator[celaut.Contract, None, None]:
    pass  # TODO