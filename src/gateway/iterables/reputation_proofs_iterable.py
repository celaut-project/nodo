"""GetReputationProofs (#652), behind the same caller gate as ListPeers."""
from protos.gateway_bee import rpc_input, rpc_output
from src.gateway.client_gate import require_caller, simple_rpc_timeout_seconds
from src.reputation_system import offchain_proof
from src.utils.bee_client import BeeClient


class ReputationProofsIterable:
    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context

    def _proofs(self):
        for proof in offchain_proof.serve():
            if not self.context.is_active():
                return
            yield proof

    def __iter__(self):
        client = BeeClient.parse_one(
            self.request_iterator, indices=rpc_input("GetReputationProofs"),
            timeout=simple_rpc_timeout_seconds(),
        )
        require_caller(self.context, client.client_id if client else "")
        yield from BeeClient.respond(self._proofs(), indices=rpc_output("GetReputationProofs"))
