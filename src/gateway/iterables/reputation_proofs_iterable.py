"""GetReputationProofs (#652), behind the same caller gate as ListPeers, and the limits of
``offchain_limits`` on top of it: a call is bounded in proofs and bytes, and a client in
calls and bytes over time."""
from protos.gateway_bee import rpc_input, rpc_output
from src.gateway.client_gate import ClientRequired, require_caller, simple_rpc_timeout_seconds
from src.reputation_system import offchain_limits, offchain_proof
from src.utils.bee_client import BeeClient


class ReputationProofsIterable:
    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context
        self.sent_bytes = 0

    def _proofs(self, caller: str, max_bytes: int):
        try:
            for proof in offchain_proof.serve(max_bytes):
                if not self.context.is_active():
                    return
                self.sent_bytes += proof.ByteSize()
                yield proof
        finally:
            offchain_limits.charge_pull(caller, self.sent_bytes)

    def __iter__(self):
        client = BeeClient.parse_one(
            self.request_iterator, indices=rpc_input("GetReputationProofs"),
            timeout=simple_rpc_timeout_seconds(),
        )
        caller = require_caller(self.context, client.client_id if client else "")
        try:
            max_bytes = offchain_limits.admit_pull(caller)
        except offchain_limits.Refused as e:
            raise ClientRequired(str(e))
        yield from BeeClient.respond(self._proofs(caller, max_bytes),
                                     indices=rpc_output("GetReputationProofs"))
