"""Verify a specific peer's on-chain reputation proofs (read-only dev command).

Given a peer id, this reads every proof the peer announced in its stored
advertisement — a node can hold more than one, and each is an opinion set it
published, not a credential we assigned it (issue #281) — and then reuses the
reputation-system validation primitives — nothing new is implemented here — to
check each of them, in order:

  0. the peer attests an Ergo wallet, and that attestation verifies: the wallet
     signed this peer's id (:func:`attested_proof_owner`). Everything below
     is about that wallet, because R7 is the reputation contract's spending
     clause and so holds an Ergo proposition, never a node identity.
  1. the proof's unspent box(es) sit on the *canonical* reputation contract
     (:func:`_boxes_off_canonical_contract`),
  2. each box carries the canonical R4/R5/R7 register layout plus a reputation
     token (:func:`_validate_box_structure`), and
  3. the attested wallet matches the R7 owner ``propositionBytes``.

Two links rather than one byte comparison, and they close the same loop: the peer
proved control of its identity key by signing its ``GetPeerInfo`` response (see
``manager.verified_peer_public_key``), that signature covers the attestation, and
the attestation is the wallet vouching for the identity.

It is strictly read-only: only Ergo-explorer reads. No transaction is built or
broadcast, and no RPC round-trip against the peer is needed anymore.
"""

import sqlite3
from typing import List, Optional

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
from src.reputation_system.proof_attestation import (
    attested_proof_owner,
    node_proposition_hex,
)


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
    """One proof, checked end to end. Prints its own reasons; returns pass/fail."""
    proof_id = get_token_id(contract)
    print(f"  Proof id: {proof_id}", flush=True)

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

    if owner_proposition_hex != node_proposition_hex(wallet_public_key):
        print(
            f"FAIL: the attested wallet {wallet_public_key} does not match the R7 owner "
            "(the proof was published by a different wallet than the one this peer "
            "proved it holds).",
            flush=True,
        )
        return False

    print(f"  OK: reputation proof {proof_id} is owned by the attested wallet.", flush=True)
    return True
