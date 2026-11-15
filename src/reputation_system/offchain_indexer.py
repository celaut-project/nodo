"""The periodic tick of off-chain reputation (#652). Never raises.

* Pull: every ``reputation.OFFCHAIN_PULL_INTERVAL_SECONDS``, ask one random peer for the
  proofs that it holds (``GetReputationProofs``), and take each one
  (``offchain_proof.receive``): its evidence is checked, and the proof is kept when its
  publisher is a peer of this node.
* Push: when the fault reports of this node changed, give the proof of this node to a few
  random peers (``PushReputationProof``), so a fault that this node proved reaches them
  before their next pull. It is bounded by ``reputation.PUSH_OFFCHAIN_FANOUT`` and
  ``reputation.PUSH_OFFCHAIN_MIN_INTERVAL_SECONDS``, and each receiver limits it too
  (``offchain_limits``).
* Score: work out the voice of each kept publisher and its scores, and store them for the
  balancer (``offchain_opinions``, read by ``offchain_credit``).
* Retention: delete the proofs of publishers that are not peers any more, and the
  evidence older than ``reputation.FAULT_EVIDENCE_RETENTION_DAYS``.
"""
import math
import random
import time
from itertools import islice
from typing import Dict, List, Tuple

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_EVIDENCE_RETENTION_DAYS = 90
RPC_TIMEOUT_SECONDS = 10

DEFAULT_PUSH_FANOUT = 3
DEFAULT_PUSH_MIN_INTERVAL_SECONDS = 300

_last_pull = None
_last_refresh = None
_last_push = None
_pushed_faults = None


def _interval(config) -> float:
    try:
        value = float(config.get("reputation.OFFCHAIN_PULL_INTERVAL_SECONDS",
                                 DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_INTERVAL_SECONDS
    return value if math.isfinite(value) and value > 0 else DEFAULT_INTERVAL_SECONDS


def pull_once(source: str) -> Tuple[int, int]:
    """Ask ``source`` for its proofs. Returns ``(received, refused)``."""
    from src.identity.grpc_transport import peer_channel
    from src.manager.manager import get_client_id_on_other_peer
    from src.database.sql_connection import SQLConnection
    from src.reputation_system import offchain_proof
    from src.utils.bee_client import BeeClient

    client_id = get_client_id_on_other_peer(peer_id=source)
    if not client_id:
        return 0, 0
    known = set(SQLConnection().get_peers_id())
    received = refused = 0
    channel = peer_channel(peer_id=source)
    try:
        replies = BeeClient.get_reputation_proofs(
            channel, client_id=client_id, timeout=RPC_TIMEOUT_SECONDS)
        try:
            # Every message counts against the limit, a refused one too: a hostile peer
            # does not get an unlimited stream by sending proofs that do not verify.
            for proof in islice(replies, offchain_proof.max_proofs()):
                why = offchain_proof.receive(proof.SerializeToString(), known=known)
                if why is None:
                    received += 1
                else:
                    refused += 1
                    LOGGER(f"[REPUTATION] refused a proof of {proof.publisher[:12]} "
                           f"from {source[:12]}: {why}")
        finally:
            close = getattr(replies, "close", None)
            if close:
                close()
    finally:
        channel.close()
    return received, refused


def pull_tick() -> bool:
    """One pull when it is due. True if a pull was done."""
    global _last_pull
    try:
        config = ConfigManager()
        if not config.get("reputation.PULL_OFFCHAIN_PROOFS", True):
            return False
        if _last_pull is not None and time.monotonic() - _last_pull < _interval(config):
            return False
        _last_pull = time.monotonic()
        from src.database.sql_connection import SQLConnection
        ids = SQLConnection().get_peers_id()
        if not ids:
            return False
        source = random.choice(ids)
        received, refused = pull_once(source)
        LOGGER(f"[REPUTATION] pulled {received} proofs from {source[:12]} "
               f"({refused} refused)")
        return True
    except Exception as e:
        # Includes peers from before #652, which answer UNIMPLEMENTED.
        LOGGER(f"[REPUTATION] pull of off-chain proofs failed: {e}")
        return True


def _fault_key(proof: celaut_pb2.ReputationProof) -> str:
    """A digest of the fault reports in ``proof``: it changes when a fault is added."""
    import hashlib

    digest = hashlib.blake2b(digest_size=16)
    for opinion in proof.opinions:
        if not opinion.amount and not opinion.positive:
            digest.update(opinion.object.encode("utf-8") + bytes(opinion.content))
    return digest.hexdigest()


def push_once(target: str, proof: celaut_pb2.ReputationProof) -> bool:
    """Give ``proof`` to ``target``. True if it took it."""
    from src.identity.grpc_transport import peer_channel
    from src.manager.manager import get_client_id_on_other_peer
    from src.utils.bee_client import BeeClient

    client_id = get_client_id_on_other_peer(peer_id=target)
    if not client_id:
        return False
    channel = peer_channel(peer_id=target)
    try:
        ack = BeeClient.push_reputation_proof(
            channel, proof, client_id=client_id, timeout=RPC_TIMEOUT_SECONDS)
    finally:
        channel.close()
    if ack is not None and not ack.accepted:
        LOGGER(f"[REPUTATION] {target[:12]} did not take the pushed proof: {ack.reason}")
    return bool(ack is not None and ack.accepted)


def push_tick() -> int:
    """Push the proof of this node to a few peers when its fault reports changed since the
    last push, and at most once each interval. Returns how many peers took it."""
    global _last_push, _pushed_faults
    try:
        config = ConfigManager()
        if not config.get("reputation.PUSH_OFFCHAIN_PROOFS", True):
            return 0
        try:
            gap = float(config.get("reputation.PUSH_OFFCHAIN_MIN_INTERVAL_SECONDS",
                                   DEFAULT_PUSH_MIN_INTERVAL_SECONDS))
            fanout = int(config.get("reputation.PUSH_OFFCHAIN_FANOUT", DEFAULT_PUSH_FANOUT))
        except (TypeError, ValueError, OverflowError):
            gap, fanout = DEFAULT_PUSH_MIN_INTERVAL_SECONDS, DEFAULT_PUSH_FANOUT
        if fanout <= 0 or not math.isfinite(gap):
            return 0
        if _last_push is not None and time.monotonic() - _last_push < max(0.0, gap):
            return 0
        from src.database.sql_connection import SQLConnection
        from src.reputation_system import offchain_proof

        proof = offchain_proof.own_proof()
        if proof is None:
            return 0
        key = _fault_key(proof)
        if key == _pushed_faults:
            return 0
        _last_push = time.monotonic()
        ids = SQLConnection().get_peers_id()
        taken = 0
        for target in random.sample(ids, min(fanout, len(ids))):
            try:
                taken += 1 if push_once(target, proof) else 0
            except Exception as e:
                LOGGER(f"[REPUTATION] push of the proof to {target[:12]} failed: {e}")
        _pushed_faults = key
        LOGGER(f"[REPUTATION] pushed the proof to {taken} of {min(fanout, len(ids))} peers")
        return taken
    except Exception as e:
        LOGGER(f"[REPUTATION] push of off-chain proofs failed: {e}")
        return 0


def opinion_rows(proofs: List[celaut_pb2.ReputationProof], local: Dict[str, float],
                 half: float) -> List[Tuple[str, str, float, float]]:
    """``(subject, publisher, verdict, weight)`` of each score in ``proofs``.

    ``local`` is our own bounded score of each peer (``onchain_indexer.local_scores``).
    The weight of a publisher is ``cred * max(0, local[publisher])``."""
    from src.reputation_system import offchain_credit, offchain_proof
    from src.reputation_system.onchain_credit import agreement

    rows = []
    for proof in proofs:
        verdicts = {
            subject: offchain_credit.verdict(amount, positive, half)
            for subject, (amount, positive) in offchain_proof.scores(proof).items()
        }
        if not verdicts:
            continue
        credibility = agreement(verdicts, local)
        voice = credibility * max(0.0, float(local.get(proof.publisher, 0.0)))
        for subject, value in verdicts.items():
            rows.append((subject, proof.publisher, value, voice))
    return rows


def refresh() -> int:
    """Score the kept proofs and store the result. Returns how many rows."""
    from src.database.sql_connection import SQLConnection
    from src.reputation_system.onchain_indexer import local_scores

    sql = SQLConnection()
    known = set(sql.get_peers_id())
    sql.delete_offchain_proofs_except(known)
    proofs = []
    for row in sql.get_offchain_proofs():
        proof = celaut_pb2.ReputationProof()
        try:
            proof.ParseFromString(row["proof"])
        except Exception:
            continue
        proofs.append(proof)
    try:
        half = float(ConfigManager().get("balancers.REPUTATION_HALF_CREDIT", 50) or 50)
    except (TypeError, ValueError):
        half = 50.0
    rows = opinion_rows(proofs, local_scores(), half)
    sql.replace_offchain_opinions(rows)
    return len(rows)


def tick() -> None:
    """The pull, the scores and the retention, when the pull is due or the last scores
    are older than the interval (our own scores move too). Never raises."""
    global _last_refresh
    from src.reputation_system.offchain_credit import forget_cached_standings

    pulled = pull_tick()
    push_tick()
    now = time.monotonic()
    if not pulled and _last_refresh is not None \
            and now - _last_refresh < _interval(ConfigManager()):
        return
    _last_refresh = now
    try:
        refresh()
    except Exception as e:
        LOGGER(f"[REPUTATION] cannot score the off-chain proofs: {e}")
    try:
        from src.database.sql_connection import SQLConnection
        days = int(ConfigManager().get("reputation.FAULT_EVIDENCE_RETENTION_DAYS",
                                       DEFAULT_EVIDENCE_RETENTION_DAYS) or 0)
        if days > 0:
            SQLConnection().prune_fault_evidence(days)
    except Exception as e:
        LOGGER(f"[REPUTATION] cannot prune the fault evidence: {e}")
    forget_cached_standings()
