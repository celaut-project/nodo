"""PushReputationProof (#652): a node gives this node one reputation proof.

The same caller gate as GetReputationProofs, and the limits of ``offchain_limits`` before
any check of the proof: a refused call costs this node no signature check and no
database read. The bytes of a proof count against the client also when the proof is
refused, so a client cannot send invalid proofs for free. The checks of the proof itself
are those of a proof that this node takes (``offchain_proof.receive``).
"""
from protos import celaut_pb2
from protos.gateway_bee import rpc_output
from src.database.sql_connection import SQLConnection
from src.gateway.client_gate import parse_with_client, require_caller
from src.reputation_system import offchain_limits, offchain_proof
from src.utils import logger as log
from src.utils.bee_client import BeeClient

sc = SQLConnection()


def _ack(reason: str = "") -> celaut_pb2.ReputationProofAck:
    return celaut_pb2.ReputationProofAck(accepted=not reason, reason=reason)


class PushReputationProofIterable:
    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context

    def _answer(self, proof, caller: str, client_id: str) -> celaut_pb2.ReputationProofAck:
        is_peer = bool(client_id) and sc.get_peer_id_by_local_client(client_id) is not None
        try:
            left = offchain_limits.admit_push(caller, is_peer)
        except offchain_limits.Refused as e:
            return _ack(str(e))
        if proof is None:
            return _ack("no proof in the call")
        raw = proof.SerializeToString()
        offchain_limits.charge_push(caller, len(raw))
        if len(raw) > left:
            return _ack("over the bytes that this client may push for now")
        try:
            why = offchain_proof.receive(raw)
        except Exception as e:
            log.LOGGER(f"[REPUTATION] cannot take a pushed proof: {e}")
            why = "not readable"
        return _ack(why or "")

    def __iter__(self):
        proof, client_id = parse_with_client(self.request_iterator, method="PushReputationProof")
        caller = require_caller(self.context, client_id)
        yield from BeeClient.respond(self._answer(proof, caller, client_id),
                                     indices=rpc_output("PushReputationProof"))
