from typing import Generator

from protos import celaut_pb2 as celaut
from src.utils.env import EnvManager

env_manager = EnvManager()
LEDGER = "ergo" # or "ergo-testnet" for Ergo testnet.
CONTRACT = """{
    SELF.R7[SigmaProp].get &&
    sigmaProp(SELF.tokens.size == 1) &&
    sigmaProp(OUTPUTS.forall { (x: Box) =>
    !(x.tokens.exists { (token: (Coll[Byte], Long)) => token._1 == SELF.tokens(0)._1 }) ||
    (
        x.R7[SigmaProp].get == SELF.R7[SigmaProp].get &&
        x.tokens.size == 1 &&
        x.propositionBytes == SELF.propositionBytes &&
        (x.R8[Boolean].get == false || x.R8[Boolean].get == true)
    )
    })
}"""

def generate_instance_proofs() -> Generator[celaut.Service.Api.ContractLedger, None, None]:
    yield celaut.Service.Api.ContractLedger(
        contract=CONTRACT.encode("utf-8"),
        contract_addr=env_manager.get_env('REPUTATION_PROOF_ID'),
        ledger=LEDGER
    )

def validate_contract_ledger(contract_ledger: celaut.Service.Api.ContractLedger) -> bool:
    return contract_ledger.ledger == LEDGER and contract_ledger.contract==CONTRACT.encode("utf-8")
