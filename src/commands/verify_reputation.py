"""Verify a specific peer's on-chain reputation proof (read-only dev command).

Given a peer id, this fetches the reputation proof (token) id recorded for that
peer and then reuses the reputation-system validation primitives — nothing new
is implemented here — to check, in order:

  1. the proof's unspent box(es) sit on the *canonical* reputation contract
     (:func:`_boxes_off_canonical_contract`),
  2. each box carries the canonical R4/R5/R7 register layout plus a reputation
     token (:func:`_validate_box_structure`), and
  3. the peer *cryptographically* controls the R7 owner ``propositionBytes`` via
     a fresh gRPC ``Gateway.SignPublicKey`` challenge
     (:func:`_challenge_peer_ownership`).

It is strictly read-only: only Ergo-explorer reads and a single gRPC challenge
round-trip against the peer. No transaction is built or broadcast.
"""

import sqlite3
from typing import Optional

from src.utils.config import ConfigManager
from src.reputation_system.contracts.ergo.proof_validation import (
    _boxes_off_canonical_contract,
    _challenge_peer_ownership,
    _decode_coll_byte_hex,
    _extract_register_value,
    _get_unspent_boxes_by_token,
    _validate_box_structure,
)


def _peer_reputation_proof_id(peer_id: str) -> Optional[str]:
    """Return the reputation proof (token) id recorded for ``peer_id``, or None."""
    database_file = ConfigManager().get("DATABASE_FILE")
    connection = sqlite3.connect(database_file)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT reputation_proof_id FROM peer WHERE id = ?",
            (peer_id,),
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    return row[0] if row and row[0] else None


def verify_reputation(peer_id: str) -> bool:
    """Verify ``peer_id``'s reputation proof end to end; print PASS/FAIL + reasons.

    Returns True only when the proof is on the canonical contract, structurally
    valid, and the peer proves ownership of the R7 owner key.
    """
    print(f"Verifying reputation proof for peer {peer_id} ...", flush=True)

    proof_id = _peer_reputation_proof_id(peer_id)
    if not proof_id:
        print(
            f"FAIL: no reputation proof id recorded for peer {peer_id}. "
            "Run 'nodo peers' to list known peers.",
            flush=True,
        )
        return False
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

    print(
        "  Challenging the peer to sign a fresh nonce (gRPC SignPublicKey) ...",
        flush=True,
    )
    if not _challenge_peer_ownership(peer_id, owner_proposition_hex):
        print(
            "FAIL: peer did not prove control of the R7 owner key "
            "(unreachable, RPC error, or signature did not verify — see node log).",
            flush=True,
        )
        return False

    print(
        f"PASS: peer {peer_id} owns reputation proof {proof_id} "
        "(canonical contract + valid structure + verified ownership challenge).",
        flush=True,
    )
    return True
