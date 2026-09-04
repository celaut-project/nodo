"""Verify a specific peer's on-chain reputation proofs (read-only dev command).

Given a peer id, this reads every proof the peer announced in its stored advertisement
-- a node can hold more than one, and each is an opinion set it published, not a
credential we assigned it (issue #281) -- and checks two things about each:

  1. the proof names an owner, and that owner signed this peer's id
     (:func:`attested_proof_owner`). Everything after is about that owner, because R7
     is the reputation contract's spending clause and so holds an Ergo proposition,
     never a node identity.
  2. the node's own verdict on that proof, from
     :func:`explain_contract_ledger` -- the canonical ledger and ErgoTree, an
     announced token id, unspent boxes on the canonical contract instance, the
     canonical R4/R5/R7 layout, and an R7 owner matching the attested one.

Step 2 is the node's function, not a second implementation of it. It used to be one,
and it had drifted: it never checked the ledger or the ErgoTree, so it printed PASS for
proofs `add_peer_instance` was refusing -- in exactly the case an operator would run
this, which is when the node has refused something and its log says only that it did.

Two links rather than one byte comparison, and they close the same loop: the peer
proved control of its identity key by signing its ``GetPeerInfo`` response (see
``manager.verified_peer_public_key``), that signature covers the attestation, and the
attestation is the owner vouching for the identity.

It is strictly read-only: only Ergo-explorer reads. No transaction is built or
broadcast, and no RPC round-trip against the peer is needed.
"""

import sqlite3
from typing import Optional

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import get_token_id
from src.reputation_system.contracts.ergo.proof_validation import (
    explain_contract_ledger,
)
from src.reputation_system.proof_attestation import attested_proof_owner


def _peer_advertisement(peer_id: str) -> Optional[celaut_pb2.Peer]:
    """The signed ``Peer`` message ``peer_id`` sent, kept verbatim, or None.

    Read out of the stored advertisement rather than out of columns of its own: it is
    what the peer actually signed, so everything checked here -- the proofs it
    announced and the wallet attestation that ties them to it -- is read from one
    object that verifies as a whole (issue #281).
    """
    database_file = ConfigManager().get("DATABASE_FILE")
    connection = sqlite3.connect(database_file)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT advertisement FROM peer WHERE id = ?", (peer_id,))
        row = cursor.fetchone()
    finally:
        connection.close()

    if not row or not row[0]:
        return None

    announced = celaut_pb2.Peer()
    try:
        announced.ParseFromString(row[0])
    except Exception as e:
        print(f"  (unreadable advertisement for {peer_id}: {e})", flush=True)
        return None
    return announced


def verify_reputation(peer_id: str) -> bool:
    """Verify every proof ``peer_id`` announced; print PASS/FAIL + reasons.

    Returns True only when *all* of them sit on the canonical contract, are
    structurally valid, and carry an R7 owner matching the Ergo wallet this peer has
    proved it holds. A peer announcing one proof it does not own is a peer claiming
    reputation that is not its own, so a single bad proof fails the peer.
    """
    print(f"Verifying reputation proofs for peer {peer_id} ...", flush=True)

    announced = _peer_advertisement(peer_id)
    if announced is None:
        print(
            f"FAIL: no stored advertisement for peer {peer_id}. "
            "Run 'nodo peers' to list known peers.",
            flush=True,
        )
        return False

    proofs = [c for c in announced.reputation_proofs if get_token_id(c)]
    if not proofs:
        print(
            f"FAIL: peer {peer_id} announced no reputation proof. "
            "Run 'nodo peers' to list known peers.",
            flush=True,
        )
        return False
    print(f"  Announced {len(proofs)} proof(s).", flush=True)

    # Each proof carries its own owner attestation, so each is checked against the
    # owner it names -- a peer's proofs need not share one.
    results = [_verify_proof(peer_id, contract) for contract in proofs]
    if all(results):
        print(
            f"PASS: peer {peer_id} owns all {len(proofs)} announced proof(s) "
            "(canonical contract + valid structure + R7 owner matches its attested "
            "wallet).",
            flush=True,
        )
        return True

    print(
        f"FAIL: {results.count(False)} of {len(proofs)} announced proof(s) did not "
        f"verify for peer {peer_id}.",
        flush=True,
    )
    return False


def _verify_proof(peer_id: str, contract) -> bool:
    """One proof, checked end to end. Prints its own reasons; returns pass/fail.

    The check itself is the node's own (:func:`explain_contract_ledger`), not a copy of
    it. This used to walk the same steps in its own words and had drifted: it never
    checked the ledger or the ErgoTree, so it passed proofs the node refused -- and it
    is the tool an operator reaches for precisely when the node has refused one.
    """
    proof_id = get_token_id(contract)
    print(f"  Proof id: {proof_id or '(none announced)'}", flush=True)

    # The owner first: without one there is nothing to check an R7 owner against. A peer
    # can hold the wallet and be unable to prove it, which is indistinguishable from
    # announcing someone else's -- so the answer is the same in both cases.
    wallet_public_key = attested_proof_owner(contract, peer_id)
    if not wallet_public_key:
        print(
            "FAIL: the proof carries no owner attestation this peer can prove, so it "
            "cannot be attributed to it.",
            flush=True,
        )
        return False
    print(f"  Attested owner: {wallet_public_key}", flush=True)

    reason = explain_contract_ledger(contract, wallet_public_key)
    if reason:
        print(f"FAIL: {reason}.", flush=True)
        return False

    print(
        f"  OK: proof {proof_id} is on the canonical contract, structurally valid, and "
        "owned by the attested wallet.",
        flush=True,
    )
    return True
