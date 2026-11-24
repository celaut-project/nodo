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

The proof also points to the proofs of its publisher on ledgers (``ledger_proofs``): one
``Contract`` for each, with the owner attestation of its wallet. The ``Peer`` does not
carry them. A pointer is not an opinion, so it has no weight here: a reader checks the
attestation (``manager.validate_reputation_proof``) and reads the opinions on the ledger.

The proof also holds a proof of work (#677): ``pow_nonce``, which the signature covers.
The work is the number of leading zero bits of ``proof_digest``. A receiver refuses a
proof with fewer bits than ``reputation.MIN_OFFCHAIN_POW_BITS``, and weighs the others by
``pow_factor``. A node looks for a nonce only when ``TARGET_OFFCHAIN_POW_BITS`` is above 0.
"""
import hashlib
import json
import threading
import time
from typing import Dict, Iterable, List, Optional

from protos import celaut_pb2
from src.identity import execution_receipt
from src.identity.node_identity import (
    canonical_contract_message,
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

# The most opinions and the most ledger pointers that a received proof may hold, and the
# most evidences in it that this node checks. More is refused, not cut: a cut proof does
# not verify.
MAX_OPINIONS = 4096
MAX_LEDGER_PROOFS = 64
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


def max_proof_age_seconds() -> int:
    """The oldest a kept proof may be (``reputation.MAX_OFFCHAIN_PROOF_AGE_DAYS``), in
    seconds. 0 is no limit."""
    try:
        return max(0, int(float(_setting("MAX_OFFCHAIN_PROOF_AGE_DAYS", 30)) * 86400))
    except (TypeError, ValueError, OverflowError):
        return 30 * 86400


def publish_fault_evidence() -> bool:
    """``reputation.PUBLISH_FAULT_EVIDENCE``: whether this node puts the evidence of the
    faults that it found itself in its proof (#658). It holds pages of the accused peer
    about the accounts of this node: amounts, ``instance_ref``, ``execution_name`` and
    receipt digests. When false, those faults go as reports with no evidence."""
    return bool(_setting("PUBLISH_FAULT_EVIDENCE", True))


def max_proofs() -> int:
    try:
        return max(0, int(_setting("MAX_OFFCHAIN_PROOFS_PER_RESPONSE", 25)))
    except (TypeError, ValueError):
        return 25


def share_proofs() -> bool:
    return bool(_setting("SHARE_OFFCHAIN_PROOFS", True))


# -- Signature --------------------------------------------------------------------------

def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def _opinion_text(opinion: celaut_pb2.ReputationOpinion) -> str:
    evidence = fault_proofs.digest(opinion.evidence) if opinion.HasField("evidence") else ""
    return "~".join((_hex(opinion.type), _hex(opinion.object), str(int(opinion.amount)),
                     "1" if opinion.positive else "0", bytes(opinion.content).hex(), evidence))


def _ledger_proofs_text(proof: celaut_pb2.ReputationProof) -> str:
    """The pointers to the proofs on ledgers, sorted so the order does not matter."""
    return "/".join(sorted(canonical_contract_message(c) for c in proof.ledger_proofs))


def pow_base(proof: celaut_pb2.ReputationProof) -> str:
    """Blake2b-256 hex of the canonical text of every field but the nonce and the
    signature: ``publisher|scheme|ts|opinion/opinion/...|pointer/pointer/...``, in the order
    of the numbers of the fields in ``celaut.proto``. An opinion is
    ``hex(type)~hex(object)~amount~positive~hex(content)~evidence``, where ``evidence`` is
    ``fault_proofs.digest`` of its evidence, or empty. The scheme is
    ``execution_receipt.canonical_scheme``. The pointers are the sorted
    ``node_identity.canonical_contract_message`` of each ``ledger_proofs``, joined by ``/``,
    after the opinions. It is computed once when a node looks for a
    nonce: only the last step of the digest depends on the nonce."""
    text = "|".join((
        proof.publisher, execution_receipt.canonical_scheme(proof.signature_scheme),
        str(int(proof.ts)),
        "/".join(_opinion_text(opinion) for opinion in proof.opinions),
        _ledger_proofs_text(proof),
    ))
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def _pow_hash(base: str, nonce: int) -> bytes:
    return hashlib.blake2b(f"{base}|{int(nonce)}".encode("utf-8"), digest_size=32).digest()


def proof_digest(proof: celaut_pb2.ReputationProof) -> str:
    """Blake2b-256 hex of ``pow_base|pow_nonce``: it covers every field but the signature.
    The nonce is the last part, and not in the place of its number: a node that looks for a
    nonce hashes the base once and then only these last bytes, for each try.
    The signature is over this digest, and the work of the proof (#677) is the number of
    leading zero bits of it (:func:`pow_bits`)."""
    return _pow_hash(pow_base(proof), proof.pow_nonce).hex()


# -- Proof of work (#677) -----------------------------------------------------------------

# A node does not look for more zero bits than this: 2^32 hashes is hours of work.
MAX_POW_BITS = 32


def zero_bits(raw: bytes) -> int:
    """The number of leading zero bits of ``raw``."""
    value = int.from_bytes(raw, "big")
    return len(raw) * 8 - value.bit_length()


def pow_bits(proof: celaut_pb2.ReputationProof) -> int:
    """The work of ``proof``: the leading zero bits of its digest. A proof with nonce 0
    has the bits of that one hash, no more than any other single try. 0 if the digest
    cannot be computed."""
    try:
        return zero_bits(bytes.fromhex(proof_digest(proof)))
    except Exception:
        return 0


def _pow_int(key: str, default: int) -> int:
    try:
        return max(0, min(MAX_POW_BITS, int(_setting(key, default))))
    except (TypeError, ValueError, OverflowError):
        return default


def min_pow_bits() -> int:
    """``reputation.MIN_OFFCHAIN_POW_BITS``: a proof with fewer bits is refused and
    dropped. 0 keeps every proof."""
    return _pow_int("MIN_OFFCHAIN_POW_BITS", 0)


def target_pow_bits() -> int:
    """``reputation.TARGET_OFFCHAIN_POW_BITS``: the bits that give a proof its whole voice.
    0 switches the weight by work off, and this node looks for no nonce."""
    return _pow_int("TARGET_OFFCHAIN_POW_BITS", 0)


def pow_factor(bits: int) -> float:
    """The factor of the voice of a proof with ``bits`` of work: ``min(1, 2^(bits -
    target))``. It is linear in the work (2^bits is the number of tries), so a publisher
    that splits its work between N identities has the same voice in all, and no more.
    1 when the weight by work is off (``target`` 0)."""
    target = target_pow_bits()
    if target <= 0:
        return 1.0
    return min(1.0, 2.0 ** (int(bits) - target))


def mine(proof: celaut_pb2.ReputationProof, bits: int,
         stop: Optional[threading.Event] = None, start: int = 0) -> bool:
    """Look for a ``pow_nonce`` that gives ``proof`` at least ``bits`` of work, and set it.
    False if ``stop`` was set first. The proof must have its ``publisher``, ``ts``,
    opinions and scheme: it is signed after (:func:`sign`)."""
    base = pow_base(proof)
    nonce = int(start)
    while True:
        if zero_bits(_pow_hash(base, nonce)) >= bits:
            proof.pow_nonce = nonce
            return True
        nonce += 1
        if stop is not None and nonce % 4096 == 0 and stop.is_set():
            return False


def sign(proof: celaut_pb2.ReputationProof) -> Optional[celaut_pb2.ReputationProof]:
    """Sign ``proof`` with the key of this node, in place. None with no identity key. The
    signature covers ``pow_nonce``: set it first."""
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
    # The proto says lowercase hex, as for a receipt (#593): a second spelling of the same
    # signature would be a second proof.
    if publisher != proof.publisher or not proof.signature \
            or proof.signature != proof.signature.lower():
        return False
    if not execution_receipt._scheme_is_ours(proof.signature_scheme):
        return False
    try:
        digest = proof_digest(proof)
    except Exception:
        # For example an ``Amount.n`` of an evidence that is not a number: a proof that
        # cannot be read is refused, and it does not stop the pull that holds it.
        return False
    return verify_peer_payload(publisher, reputation_proof_payload(publisher, digest),
                               proof.signature)


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
        if row.get("source") == "own" and not publish_fault_evidence():
            # The pages show the accounts of this node. The fault is reported with no
            # evidence, as a fault that rests on what only this node knows (#658).
            opinions.append(celaut_pb2.ReputationOpinion(
                type=node_type(), object=row["accused"], amount=0, positive=False,
                content=_fault_content(*key[1:])))
            continue
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


def own_pow_bits() -> int:
    """The bits that this node looks for in its own proof: the target, and never fewer than
    the minimum that it asks of others. 0 when both are 0: it looks for no nonce."""
    return max(target_pow_bits(), min_pow_bits())


def _own_ledger_proofs() -> List[celaut_pb2.Contract]:
    """The pointers to the proofs of this node on ledgers, each with the attestation of its
    wallet over the ``peer_id`` of this node (``fetch.local_proofs``). An error gives none:
    the proof of this node is still built and signed."""
    from src.reputation_system.fetch import local_proofs

    try:
        return list(local_proofs())[:MAX_LEDGER_PROOFS]
    except Exception as e:
        log.LOGGER(f"Cannot read the ledger proofs of this node: {e}")
        return []


def build_own_proof(mine_bits: Optional[int] = None) -> Optional[celaut_pb2.ReputationProof]:
    """The signed proof of this node now, or None with no identity key.

    With ``mine_bits`` above 0 the nonce is looked for here, and the call takes the time
    that it takes (``own_proof`` does it in a thread). With None the proof has nonce 0."""
    from src.database.sql_connection import SQLConnection

    sql = SQLConnection()
    me = get_node_public_key_hex() or ""
    proof = celaut_pb2.ReputationProof(ts=int(time.time()))
    proof.opinions.extend(_score_opinions(sql, me))
    proof.opinions.extend(_fault_opinions(sql, me))
    proof.ledger_proofs.extend(_own_ledger_proofs())
    if mine_bits:
        proof.publisher = me
        proof.signature_scheme.CopyFrom(execution_receipt.receipt_scheme())
        mine(proof, mine_bits)
    return sign(proof)


def _content_key(proof: celaut_pb2.ReputationProof) -> str:
    """What the proof says, with no date and no nonce: two proofs with the same key need no
    new work."""
    return hashlib.blake2b(
        ("/".join(_opinion_text(opinion) for opinion in proof.opinions)
         + "|" + _ledger_proofs_text(proof)).encode("utf-8"),
        digest_size=16).hexdigest()


def _reuse_seconds() -> int:
    """How long a proof that said the same may stay: half of the maximum age, so a peer
    does not drop it, and one day when the age has no limit."""
    age = max_proof_age_seconds()
    return age // 2 if age else 86400


_own_lock = threading.Lock()
_own_cache: Dict[str, object] = {"at": 0.0, "proof": None, "key": None, "stop": None}


def _mine_in_background(unsigned: celaut_pb2.ReputationProof, key: str, bits: int,
                        stop: threading.Event) -> None:
    """Look for the nonce of ``unsigned`` in a thread, sign it, and make it the proof of
    this node when nothing newer took its place."""
    def work():
        try:
            proof = celaut_pb2.ReputationProof()
            proof.CopyFrom(unsigned)
            if not mine(proof, bits, stop=stop) or sign(proof) is None:
                return
            with _own_lock:
                if _own_cache["stop"] is stop and not stop.is_set():
                    _own_cache.update(proof=proof, key=key)
        except Exception as e:
            log.LOGGER(f"Cannot look for the work of the reputation proof: {e}")

    threading.Thread(target=work, name="offchain-pow", daemon=True).start()


def own_proof() -> Optional[celaut_pb2.ReputationProof]:
    """The proof of this node, built again at most every ``OWN_PROOF_SECONDS``. A proof
    is signed once for many callers, and its ``ts`` grows only when it is built again.

    With work to do (``own_pow_bits`` above 0) a proof that says the same as the last one
    is kept, and so is its nonce, until it is old (``_reuse_seconds``). A proof that
    changed is mined in a thread, and the last proof is given until the new one is ready:
    the tick never waits for the work (#677)."""
    with _own_lock:
        now = time.monotonic()
        cached = _own_cache["proof"]
        if cached is not None and now - float(_own_cache["at"]) < OWN_PROOF_SECONDS:
            return cached
        bits = own_pow_bits()
        try:
            fresh = build_own_proof()
        except Exception as e:
            log.LOGGER(f"Cannot build the reputation proof of this node: {e}")
            return cached
        if fresh is None:
            return cached
        if not bits:
            _own_cache.update(at=now, proof=fresh, key=None)
            return fresh
        key = _content_key(fresh)
        if cached is not None and key == _own_cache["key"] \
                and time.time() - cached.ts < _reuse_seconds():
            _own_cache.update(at=now)
            return cached
        previous_stop = _own_cache["stop"]
        if previous_stop is not None:
            previous_stop.set()
        stop = threading.Event()
        # No proof yet: give the one with nonce 0 now. It is below the bits, and a peer
        # that asks for them refuses it, but the node has one to answer with.
        if cached is None:
            _own_cache.update(proof=fresh, key=None)
        _own_cache.update(at=now, stop=stop)
        unsigned = celaut_pb2.ReputationProof()
        unsigned.CopyFrom(fresh)
        unsigned.ClearField("signature")
        _mine_in_background(unsigned, key, bits, stop)
        return _own_cache["proof"]


def forget_own_proof() -> None:
    with _own_lock:
        stop = _own_cache["stop"]
        if stop is not None:
            stop.set()
        _own_cache.update(at=0.0, proof=None, key=None, stop=None)


# -- Proofs of other nodes --------------------------------------------------------------

def serve(max_bytes: Optional[int] = None) -> Iterable[celaut_pb2.ReputationProof]:
    """What ``GetReputationProofs`` sends: the proof of this node, then the kept proofs of
    other nodes as they were received, at most ``MAX_OFFCHAIN_PROOFS_PER_RESPONSE`` in
    all, and at most ``max_bytes`` bytes (default ``MAX_OFFCHAIN_BYTES_PER_RESPONSE``).
    The proof of this node always goes first, and a proof that does not fit is skipped, not
    cut. Nothing when ``SHARE_OFFCHAIN_PROOFS`` is false."""
    from src.database.sql_connection import SQLConnection
    from src.reputation_system import offchain_limits

    if not share_proofs():
        return
    limit = max_proofs()
    if limit <= 0:
        return
    if max_bytes is None:
        max_bytes = offchain_limits.setting("MAX_OFFCHAIN_BYTES_PER_RESPONSE")
    sent = used = 0
    own = own_proof()
    if own is not None:
        yield own
        sent += 1
        used += own.ByteSize()
    if sent >= limit:
        return
    sql = SQLConnection()
    # Sizes first: the proofs are read only when they fit, so a call costs what it sends.
    for sample in sql.get_offchain_proof_samples(limit - sent):
        if used + int(sample["size"] or 0) > max_bytes:
            continue
        raw = sql.get_offchain_proof(sample["publisher"])
        if raw is None or used + len(raw) > max_bytes:
            continue
        proof = celaut_pb2.ReputationProof()
        try:
            proof.ParseFromString(raw)
        except Exception:
            continue
        used += len(raw)
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
    sql = SQLConnection()
    # A fault that this node scored itself (``statement_faults``, with no evidence then)
    # is not scored again when the same fault comes with its evidence.
    scored_here = sql.statement_fault_exists(
        proven.accused, proven.account_ref, proven.reason, proven.seq)
    if not sql.record_fault_evidence(
            proven.accused, proven.reason, proven.account_ref, proven.seq,
            fault_proofs.digest(evidence), evidence.SerializeToString(), "received"):
        return False
    if scored_here:
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
    if len(proof.ledger_proofs) > MAX_LEDGER_PROOFS:
        return "too many ledger proofs"
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
    age = max_proof_age_seconds()
    if age and proof.ts < time.time() - age:
        return "too old"
    if pow_bits(proof) < min_pow_bits():
        return "not enough work"
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
    check_ledger_proofs(proof)
    sql.store_offchain_proof(proof.publisher, proof.ts, raw)
    return None


def ledger_proofs_of(raw: Optional[bytes]) -> List[celaut_pb2.Contract]:
    """The ``ledger_proofs`` of a kept proof, from its bytes (the ``proof`` column of
    ``offchain_proofs``). None, or bytes that cannot be read, give none."""
    if not raw:
        return []
    proof = celaut_pb2.ReputationProof()
    try:
        proof.ParseFromString(bytes(raw))
    except Exception:
        return []
    return list(proof.ledger_proofs)


def stored_ledger_proofs(peer_id: str) -> List[celaut_pb2.Contract]:
    """The pointers to the proofs on ledgers that ``peer_id`` gave in its kept proof, or
    none: this node holds a proof only after a pull or a push took it, and only of a peer."""
    from src.database.sql_connection import SQLConnection

    return ledger_proofs_of(SQLConnection().get_offchain_proof((peer_id or "").lower()))


def check_ledger_proofs(proof: celaut_pb2.ReputationProof) -> None:
    """Log each pointer of ``proof`` that its publisher does not own. Nothing is refused:
    the opinions of the proof are credited on their own, and a pointer that fails is only
    a pointer that a reader does not credit (``manager.validate_reputation_proof`` logs
    why)."""
    from src.manager.manager import validate_reputation_proof

    for contract in proof.ledger_proofs:
        try:
            validate_reputation_proof(contract_ledger=contract, peer_id=proof.publisher)
        except Exception as e:
            log.LOGGER(f"Uncontrolled error validating a ledger proof of "
                       f"{proof.publisher}: {e}")


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


def ledger_fault_reports(limit: Optional[int] = None) -> List[tuple]:
    """The faults that this node holds the evidence of, as ``(accused, R9 text)`` for the
    boxes of the proof on the ledger (#652): the newest first, at most
    ``reputation.MAX_FAULT_REPORTS_ON_LEDGER`` (default 0: none, #660). R9 is JSON with ``reason``,
    ``account_ref``, ``seq`` and ``evidence``, the digest of the evidence
    (``fault_proofs.digest``): a reader gets the evidence off the chain. A box pays rent
    for each byte, so the evidence itself does not go on the ledger."""
    from src.database.sql_connection import SQLConnection

    if limit is None:
        try:
            limit = max(0, int(_setting("MAX_FAULT_REPORTS_ON_LEDGER", 0)))
        except (TypeError, ValueError):
            limit = 0
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
        if row.get("source") == "own" and not publish_fault_evidence():
            # The box would point at evidence that this node does not publish (#658).
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
