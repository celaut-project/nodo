"""The off-chain reputation proof of a node (#652).

A ``ReputationProof`` holds the same opinions as a reputation proof on a ledger
(``docs/ERGO.md``), signed with the identity key of the node: the ``peer_id`` is the id of
the proof and its owner. Nobody pays for it, so it has no weight by itself: a receiver
weighs each publisher by its own experience of it (``offchain_credit``).

The opinions of this node are:

* one score for each peer: ``amount`` is ``|r|``, the event log of this node for that
  peer (``interface.compute_reputation``), and ``positive`` is ``r >= 0``;
* one report for each fault that this node scored in a statement of a peer
  (``statement_faults``), and for each fault that it holds the evidence of
  (``fault_evidence``): ``amount`` 0, ``content`` the reason as JSON, and the evidence
  when ``fault_proofs`` can prove the fault from it. The score already counts the fault.

The signature covers a canonical text of every field, never ``SerializeToString()``,
as for a statement: ``"celaut-reputation-proof:" + publisher + "|" + proof_digest``.
"""
import hashlib
import json
import threading
import time
from typing import Dict, Iterable, List, Optional

from protos import celaut_pb2
from src.identity import execution_receipt
from src.identity.node_identity import (
    get_node_public_key_hex,
    normalize_public_key_hex,
    reputation_proof_payload,
    sign_peer_payload,
    verify_peer_payload,
)
from src.reputation_system import fault_proofs
from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()

# The type of an opinion about a node: the same id as R4 on Ergo. Empty when the config
# does not set it, and then the type is the text below, so an opinion always has one.
NODE_TYPE_FALLBACK = "celaut-node"

# The most opinions that a received proof may hold, and the most evidences in it that
# this node checks. More is refused, not cut: a cut proof does not verify.
MAX_OPINIONS = 4096
MAX_EVIDENCES_CHECKED = 64

# The ledger tag of an opinion that comes from an off-chain proof, in reports.
OFFCHAIN_LEDGER = "offchain"

# How long the proof of this node is kept before it is built again.
OWN_PROOF_SECONDS = 300


def node_type() -> str:
    return str(env_manager.get("ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID", "")
               or NODE_TYPE_FALLBACK)


def _setting(key: str, default):
    value = env_manager.get(f"reputation.{key}", default)
    return default if value is None else value


def max_proof_bytes() -> int:
    try:
        return max(0, int(_setting("MAX_OFFCHAIN_PROOF_BYTES", 1048576)))
    except (TypeError, ValueError):
        return 1048576


def max_fault_reports() -> int:
    try:
        return max(0, int(_setting("MAX_FAULT_REPORTS", 64)))
    except (TypeError, ValueError):
        return 64


def max_proofs() -> int:
    try:
        return max(0, int(_setting("MAX_OFFCHAIN_PROOFS_PER_RESPONSE", 100)))
    except (TypeError, ValueError):
        return 100


def share_proofs() -> bool:
    return bool(_setting("SHARE_OFFCHAIN_PROOFS", True))


# -- Signature --------------------------------------------------------------------------

def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def _opinion_text(opinion: celaut_pb2.ReputationOpinion) -> str:
    evidence = fault_proofs.digest(opinion.evidence) if opinion.HasField("evidence") else ""
    return "~".join((_hex(opinion.type), _hex(opinion.object), str(int(opinion.amount)),
                     "1" if opinion.positive else "0", bytes(opinion.content).hex(), evidence))


def proof_digest(proof: celaut_pb2.ReputationProof) -> str:
    """Blake2b-256 hex of the canonical text of every field but the signature.

    ``publisher|ts|scheme|opinion/opinion/...``. An opinion is ``hex(type)~hex(object)
    ~amount~positive~hex(content)~evidence``, where ``evidence`` is
    ``fault_proofs.digest`` of its evidence, or empty. The scheme is
    ``execution_receipt.canonical_scheme``.
    """
    text = "|".join((
        proof.publisher, str(int(proof.ts)),
        execution_receipt.canonical_scheme(proof.signature_scheme),
        "/".join(_opinion_text(opinion) for opinion in proof.opinions),
    ))
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def sign(proof: celaut_pb2.ReputationProof) -> Optional[celaut_pb2.ReputationProof]:
    """Sign ``proof`` with the key of this node, in place. None with no identity key."""
    publisher = get_node_public_key_hex()
    if not publisher:
        return None
    proof.publisher = publisher
    proof.signature_scheme.CopyFrom(execution_receipt.receipt_scheme())
    signature = sign_peer_payload(reputation_proof_payload(publisher, proof_digest(proof)))
    if not signature:
        return None
    proof.signature = signature
    return proof


def verify(proof: celaut_pb2.ReputationProof) -> bool:
    """Whether ``proof.publisher`` signed exactly this proof, in a scheme of this node."""
    try:
        publisher = normalize_public_key_hex(proof.publisher)
    except Exception:
        return False
    if publisher != proof.publisher or not proof.signature:
        return False
    if not execution_receipt._scheme_is_ours(proof.signature_scheme):
        return False
    return verify_peer_payload(
        publisher, reputation_proof_payload(publisher, proof_digest(proof)), proof.signature)


# -- The proof of this node -------------------------------------------------------------

def _score_opinions(sql, exclude: str) -> List[celaut_pb2.ReputationOpinion]:
    from src.reputation_system.interface import compute_reputation

    opinions = []
    for peer_id in sorted(sql.get_peers_id()):
        if not peer_id or peer_id == exclude:
            continue
        score = int(compute_reputation(peer_id))
        if score == 0:
            continue
        opinions.append(celaut_pb2.ReputationOpinion(
            type=node_type(), object=peer_id, amount=abs(score), positive=score > 0))
    return opinions


def _fault_content(reason: str, account_ref: str, seq: int) -> bytes:
    return json.dumps({"reason": reason, "account_ref": account_ref, "seq": int(seq)},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fault_opinions(sql, exclude: str) -> List[celaut_pb2.ReputationOpinion]:
    """The fault reports: first the faults with evidence, then the faults without it,
    the newest first, at most ``reputation.MAX_FAULT_REPORTS`` in all."""
    limit = max_fault_reports()
    seen, opinions = set(), []
    for row in sql.get_fault_evidence(limit):
        key = (row["accused"], row["reason"], row["account_ref"], int(row["seq"]))
        evidence = celaut_pb2.FaultEvidence()
        try:
            evidence.ParseFromString(row["evidence"])
        except Exception:
            continue
        if row["accused"] == exclude or key in seen:
            continue
        seen.add(key)
        opinions.append(celaut_pb2.ReputationOpinion(
            type=node_type(), object=row["accused"], amount=0, positive=False,
            content=_fault_content(*key[1:]), evidence=evidence))
    for row in sql.get_statement_faults(limit):
        if len(opinions) >= limit:
            break
        key = (row["peer_id"], row["reason"], row["account_ref"], int(row["seq"]))
        if row["peer_id"] == exclude or key in seen:
            continue
        seen.add(key)
        opinions.append(celaut_pb2.ReputationOpinion(
            type=node_type(), object=row["peer_id"], amount=0, positive=False,
            content=_fault_content(*key[1:])))
    return opinions[:limit]


def build_own_proof() -> Optional[celaut_pb2.ReputationProof]:
    """The signed proof of this node now, or None with no identity key."""
    from src.database.sql_connection import SQLConnection

    sql = SQLConnection()
    me = get_node_public_key_hex() or ""
    proof = celaut_pb2.ReputationProof(ts=int(time.time()))
    proof.opinions.extend(_score_opinions(sql, me))
    proof.opinions.extend(_fault_opinions(sql, me))
    return sign(proof)


_own_lock = threading.Lock()
_own_cache: Dict[str, object] = {"at": 0.0, "proof": None}


def own_proof() -> Optional[celaut_pb2.ReputationProof]:
    """The proof of this node, built again at most every ``OWN_PROOF_SECONDS``. A proof
    is signed once for many callers, and its ``ts`` grows only when it is built again."""
    with _own_lock:
        now = time.monotonic()
        if _own_cache["proof"] is None or now - float(_own_cache["at"]) >= OWN_PROOF_SECONDS:
            try:
                proof = build_own_proof()
            except Exception as e:
                log.LOGGER(f"Cannot build the reputation proof of this node: {e}")
                proof = None
            if proof is not None:
                _own_cache.update(at=now, proof=proof)
        return _own_cache["proof"]


def forget_own_proof() -> None:
    with _own_lock:
        _own_cache.update(at=0.0, proof=None)


# -- Proofs of other nodes --------------------------------------------------------------

def serve() -> Iterable[celaut_pb2.ReputationProof]:
    """What ``GetReputationProofs`` sends: the proof of this node, then the kept proofs of
    other nodes as they were received, at most ``MAX_OFFCHAIN_PROOFS_PER_RESPONSE`` in
    all. Nothing when ``SHARE_OFFCHAIN_PROOFS`` is false."""
    from src.database.sql_connection import SQLConnection

    if not share_proofs():
        return
    limit = max_proofs()
    if limit <= 0:
        return
    sent = 0
    own = own_proof()
    if own is not None:
        yield own
        sent += 1
    if sent >= limit:
        return
    for row in SQLConnection().get_offchain_proofs(limit - sent):
        proof = celaut_pb2.ReputationProof()
        try:
            proof.ParseFromString(row["proof"])
        except Exception:
            continue
        yield proof


def apply_evidence(evidence: celaut_pb2.FaultEvidence, me: str, known: set) -> bool:
    """Score the fault that ``evidence`` proves, as a fault that this node saw itself
    (#652), once for each fault. Only a fault of a peer of this node, and never of this
    node. True if it was scored now."""
    from src.database.sql_connection import SQLConnection
    from src.reputation_system import interface

    accused = (evidence.accused or "").lower()
    if not accused or accused == me or accused not in known:
        return False
    proven = fault_proofs.check(evidence)
    if proven is None:
        return False
    if not SQLConnection().record_fault_evidence(
            proven.accused, proven.reason, proven.account_ref, proven.seq,
            fault_proofs.digest(evidence), evidence.SerializeToString(), "received"):
        return False
    log.LOGGER(f"[REPUTATION] A received proof shows that {accused} did {proven.reason} "
               f"at movement {proven.seq}: {proven.problem}")
    interface.update_peer_reputation(peer_id=accused, amount=proven.penalty,
                                     reason=proven.reason)
    return True


def receive(raw: bytes, known: Optional[set] = None) -> Optional[str]:
    """Take a proof that a peer sent. Returns why it was refused, or None.

    The proof must be at most ``MAX_OFFCHAIN_PROOF_BYTES``, signed by its publisher, and
    newer than the kept proof of the same publisher. It is kept only when the publisher is
    a peer of this node: only a peer can have a weight. The evidence in it is checked in
    every case, because valid evidence needs no trust in who sends it.
    """
    from src.database.sql_connection import SQLConnection

    if len(raw) > max_proof_bytes():
        return "too large"
    proof = celaut_pb2.ReputationProof()
    try:
        proof.ParseFromString(raw)
    except Exception:
        return "not readable"
    if len(proof.opinions) > MAX_OPINIONS:
        return "too many opinions"
    if not verify(proof):
        return "not signed by its publisher"
    me = (get_node_public_key_hex() or "").lower()
    if proof.publisher == me:
        return "the proof of this node"
    sql = SQLConnection()
    if known is None:
        known = set(sql.get_peers_id())
    if proof.ts > time.time() + 300:
        return "dated in the future"
    last = sql.offchain_proof_ts(proof.publisher)
    if last is not None and proof.ts <= last:
        return "not newer than the kept proof"
    checked = 0
    for opinion in proof.opinions:
        if not opinion.HasField("evidence") or checked >= MAX_EVIDENCES_CHECKED:
            continue
        checked += 1
        try:
            apply_evidence(opinion.evidence, me, known)
        except Exception as e:
            log.LOGGER(f"Cannot check the evidence in the proof of {proof.publisher}: {e}")
    if proof.publisher not in known:
        return None
    sql.store_offchain_proof(proof.publisher, proof.ts, raw)
    return None


def scores(proof: celaut_pb2.ReputationProof) -> Dict[str, tuple]:
    """``{subject: (amount, positive)}`` of the score opinions of a proof about nodes,
    without fault reports and without the publisher itself. A subject with more than one
    score keeps the sum of the signed amounts."""
    out: Dict[str, int] = {}
    kind = node_type()
    for opinion in proof.opinions:
        if opinion.type != kind or not opinion.amount or opinion.object == proof.publisher:
            continue
        signed = int(opinion.amount) if opinion.positive else -int(opinion.amount)
        out[opinion.object] = out.get(opinion.object, 0) + signed
    return {subject: (abs(total), total >= 0) for subject, total in out.items() if total}


def fault_reports(proof: celaut_pb2.ReputationProof) -> List[dict]:
    """The fault reports of a proof: ``accused``, ``reason`` and ``proven`` (the evidence
    proves it)."""
    reports = []
    for opinion in proof.opinions:
        if opinion.amount or opinion.positive:
            continue
        try:
            content = json.loads(bytes(opinion.content).decode("utf-8") or "{}")
        except ValueError:
            content = {}
        reason = content.get("reason") if isinstance(content, dict) else None
        if not reason:
            continue
        proven = opinion.HasField("evidence") and fault_proofs.check(opinion.evidence) is not None
        reports.append({"accused": opinion.object, "reason": str(reason), "proven": proven})
    return reports


def ledger_fault_reports(limit: Optional[int] = None) -> List[tuple]:
    """The faults that this node holds the evidence of, as ``(accused, R9 text)`` for the
    boxes of the proof on the ledger (#652): the newest first, at most
    ``reputation.MAX_FAULT_REPORTS_ON_LEDGER``. R9 is JSON with ``reason``,
    ``account_ref``, ``seq`` and ``evidence``, the digest of the evidence
    (``fault_proofs.digest``): a reader gets the evidence off the chain. A box pays rent
    for each byte, so the evidence itself does not go on the ledger."""
    from src.database.sql_connection import SQLConnection

    if limit is None:
        try:
            limit = max(0, int(_setting("MAX_FAULT_REPORTS_ON_LEDGER", 8)))
        except (TypeError, ValueError):
            limit = 8
    if limit <= 0:
        return []
    me = get_node_public_key_hex() or ""
    reports = []
    try:
        rows = SQLConnection().get_fault_evidence(limit)
    except Exception as e:
        log.LOGGER(f"Cannot read the fault evidence for the ledger: {e}")
        return []
    for row in rows:
        if row["accused"] == me:
            continue
        evidence = celaut_pb2.FaultEvidence()
        try:
            evidence.ParseFromString(row["evidence"])
        except Exception:
            continue
        reports.append((row["accused"], json.dumps({
            "reason": row["reason"], "account_ref": row["account_ref"],
            "seq": int(row["seq"]), "evidence": fault_proofs.digest(evidence),
        }, sort_keys=True, separators=(",", ":"))))
    return reports


def opinions_about(node_id: str) -> list:
    """The scores about ``node_id`` in the kept proofs of other nodes, as ``Opinion``s
    for ``nodo reputation`` (ledger ``offchain``). The share is the amount over the sum
    of the amounts of the proof, as on a ledger. Nothing is burned behind them."""
    from src.database.sql_connection import SQLConnection
    from src.reputation_system.opinions import Opinion

    node_id = (node_id or "").lower()
    out = []
    for row in SQLConnection().get_offchain_proofs():
        proof = celaut_pb2.ReputationProof()
        try:
            proof.ParseFromString(row["proof"])
        except Exception:
            continue
        found = scores(proof)
        if node_id not in found:
            continue
        amount, positive = found[node_id]
        assigned = sum(total for total, _ in found.values())
        out.append(Opinion(
            ledger=OFFCHAIN_LEDGER, proof_id=proof.publisher, owner=proof.publisher,
            amount=int(amount), assigned_amount=int(assigned), positive=positive,
            published_at=int(proof.ts), box_id="", burned_nanoerg=0))
    return out
