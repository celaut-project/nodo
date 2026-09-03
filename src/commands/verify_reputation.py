"""Verify a specific peer's on-chain reputation proofs (read-only dev command).

Given a peer id, this reads every proof the peer announced in its stored
advertisement — a node can hold more than one, and each is an opinion set it
published, not a credential we assigned it (issue #281) — and then reuses the
reputation-system validation primitives — nothing new is implemented here — to
check each of them, in order:

  1. the proof's unspent box(es) sit on the *canonical* reputation contract
     (:func:`_boxes_off_canonical_contract`),
  2. each box carries the canonical R4/R5/R7 register layout plus a reputation
     token (:func:`_validate_box_structure`), and
  3. the peer's identity public key (its ``peer_id``, since issue #236) matches
     the R7 owner ``propositionBytes`` -- a direct byte comparison, since the
     peer already proved control of that public key by signing its
     ``GetPeerInfo`` response (see ``manager.verified_peer_public_key``).

It is strictly read-only: only Ergo-explorer reads. No transaction is built or
broadcast, and no RPC round-trip against the peer is needed anymore.
"""

import sqlite3
from typing import List

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import get_token_id
from src.reputation_system.contracts.ergo.proof_validation import (
    _boxes_off_canonical_contract,
    _decode_coll_byte_hex,
    _extract_register_value,
    _get_unspent_boxes_by_token,
    _validate_box_structure,
)
from src.reputation_system.node_identity import node_proposition_hex


def _peer_reputation_proof_ids(peer_id: str) -> List[str]:
    """Every reputation proof (token) id ``peer_id`` announced, in announced order.

    Read out of the peer's stored advertisement rather than a column of its own: the
    advertisement is the signed message the peer sent, kept verbatim, and it carries
    as many proofs as the peer holds (issue #281).
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
        return []

    announced = celaut_pb2.Peer()
    try:
        announced.ParseFromString(row[0])
    except Exception as e:
        print(f"  (unreadable advertisement for {peer_id}: {e})", flush=True)
        return []

    return [
        token_id for token_id in (
            get_token_id(contract) for contract in announced.reputation_proofs
        ) if token_id
    ]


def verify_reputation(peer_id: str) -> bool:
    """Verify every proof ``peer_id`` announced; print PASS/FAIL + reasons.

    Returns True only when *all* of them sit on the canonical contract, are
    structurally valid, and carry an R7 owner matching the peer's identity key. A
    peer announcing one proof it does not own is a peer claiming reputation that is
    not its own, so a single bad proof fails the peer.
    """
    print(f"Verifying reputation proofs for peer {peer_id} ...", flush=True)

    proof_ids = _peer_reputation_proof_ids(peer_id)
    if not proof_ids:
        print(
            f"FAIL: peer {peer_id} announced no reputation proof. "
            "Run 'nodo peers' to list known peers.",
            flush=True,
        )
        return False
    print(f"  Announced {len(proof_ids)} proof(s).", flush=True)

    results = [_verify_proof(peer_id, proof_id) for proof_id in proof_ids]
    if all(results):
        print(
            f"PASS: peer {peer_id} owns all {len(proof_ids)} announced proof(s) "
            "(canonical contract + valid structure + R7 owner matches its identity key).",
            flush=True,
        )
        return True

    print(
        f"FAIL: {results.count(False)} of {len(proof_ids)} announced proof(s) did not "
        f"verify for peer {peer_id}.",
        flush=True,
    )
    return False


def _verify_proof(peer_id: str, proof_id: str) -> bool:
    """One proof, checked end to end. Prints its own reasons; returns pass/fail."""
    print(f"  Proof id: {proof_id}", flush=True)

    boxes = _get_unspent_boxes_by_token(proof_id)
    if not boxes:
        print(
            f"FAIL: no unspent boxes found on-chain for proof id {proof_id}.",
            flush=True,
        )
        return False
    print(f"  Found {len(boxes)} unspent box(es) for the proof.", flush=True)

    off_contract = _boxes_off_canonical_contract(boxes)
    if off_contract:
        print(
            "FAIL: proof box(es) are not on the canonical reputation contract "
            f"(off-contract ErgoTrees: {[t[:16] + '...' for t in off_contract]}).",
            flush=True,
        )
        return False

    for index, box in enumerate(boxes):
        if not _validate_box_structure(box):
            print(
                f"FAIL: box #{index} lacks the canonical R4/R5/R7 reputation "
                "register structure (see node log for the offending register).",
                flush=True,
            )
            return False
    print("  Box structure OK (canonical R4/R5/R7 + reputation token).", flush=True)

    owner_propositions = {
        _decode_coll_byte_hex(str(_extract_register_value(box, "R7") or ""))
        for box in boxes
    }
    owner_propositions.discard(None)
    if len(owner_propositions) != 1:
        print(
            "FAIL: proof boxes carry inconsistent or missing R7 owner "
            f"propositionBytes ({sorted(p for p in owner_propositions if p)}).",
            flush=True,
        )
        return False
    owner_proposition_hex = owner_propositions.pop()
    print(f"  R7 owner propositionBytes: {owner_proposition_hex[:24]}...", flush=True)

    if owner_proposition_hex != node_proposition_hex(peer_id):
        print(
            f"FAIL: peer {peer_id}'s identity public key does not match the R7 owner "
            "(the proof was published by a different key than the one this peer "
            "signs its GetPeerInfo with).",
            flush=True,
        )
        return False

    print(f"  OK: peer {peer_id} owns reputation proof {proof_id}.", flush=True)
    return True
