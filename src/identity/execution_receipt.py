"""Execution receipts (#419, docs/EXECUTION_RECEIPTS.md): what a node signs about one
execution that it runs, so that a client can hold it up against the node.

A receipt is an ``ExecutionReceipt`` of one of three kinds:

* ``start``: in the answer of ``StartService``. It names the service, a salted digest
  of the environment variables the service got, and what the start charged.
* ``checkpoint``: on a ``ServiceTunnel`` stream (index 2) or an ``Observe`` stream
  (kind ``receipt``), from time to time. It gives the counters and two hash chains of
  the stream: one over the events into the instance, one over the events out of it.
* ``close``: the last checkpoint of a stream, sent when the stream stops normally.

Everything here is the reference for another implementation (the chatui client is in
Rust), so each encoding is spelled out next to the code that builds it, the same text
is in the celaut-gateway layer (``transport_stack``), and
``tests/identity/test_execution_receipt.py`` fixes test vectors: a fixed key gives fixed
bytes and a fixed signature.

The conventions are those of the identity path and of the balance statement:

* The signed text is ``celaut-execution-receipt:<peer_id>|<receipt_digest>``
  (``node_identity.receipt_payload``), UTF-8, signed with the identity key itself.
* ``receipt_digest`` is Blake2b-256 over a canonical text built field by field, never
  over ``SerializeToString()``, which protobuf does not make canonical.
* Every free text is in lowercase hex of its UTF-8, every number in decimal, every
  hash in lowercase hex. :func:`problems` refuses a hash field that is not hex, so no
  field can carry the ``|`` that separates them, and the text has one reading only.

Verification needs no database, no config and no daemon: :func:`problems`,
:func:`verify`, :func:`check_sequence` and the chain functions only read the message.
"""
import hashlib
import os
import string
import threading
import time
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from protos import celaut_pb2
from src.identity.node_identity import (
    SIGNATURE_SCHEME_COMPONENTS,
    normalize_public_key_hex,
    receipt_payload,
    same_component,
)

KIND_START = "start"
KIND_CHECKPOINT = "checkpoint"
KIND_CLOSE = "close"
KINDS = (KIND_START, KIND_CHECKPOINT, KIND_CLOSE)

PLANE_TUNNEL = "tunnel"
PLANE_OBSERVE = "observe"
PLANES = (PLANE_TUNNEL, PLANE_OBSERVE)

# What a stream lets the node see (docs/EXECUTION_RECEIPTS.md, "four planes"). A reader
# must not take a receipt for more than its coverage says.
COVERAGE_CLIENT_TUNNEL = "client-tunnel"
# The packets that the capture got, with the bytes it holds. Not "full": the capture
# keeps at most its snap length of a frame, and the kernel drops packets when the
# reader is slow. So this coverage is not proof that the instance sent nothing else.
COVERAGE_TAP = "instance-tap"
COVERAGE_COUNTERS = "counters-only"
COVERAGES = (COVERAGE_CLIENT_TUNNEL, COVERAGE_TAP, COVERAGE_COUNTERS)
# The coverages that each plane can have.
PLANE_COVERAGES = {
    PLANE_TUNNEL: (COVERAGE_CLIENT_TUNNEL,),
    PLANE_OBSERVE: (COVERAGE_TAP, COVERAGE_COUNTERS),
}

# The two chains of a stream. "in" is into the instance (caller -> service, or a packet
# with direction IN), "out" is out of it.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

# Where an environment variable came from. Today every entry is "client": the node puts
# its network resolution and its dependency wiring in /__config__, not in the env map.
# "node" is there so that a variable the node adds later is told apart from a requested
# one, as the doc asks.
ORIGIN_CLIENT = "client"
ORIGIN_NODE = "node"

# The head of a chain with no event yet.
CHAIN_GENESIS = "0" * 64

ENVS_SALT_BYTES = 16
SESSION_BYTES = 16

# How often a stream gets a checkpoint, whichever comes first, and only when an event
# came after the last one: an idle stream gets none. Read from `receipts.*` in config.
DEFAULT_CHECKPOINT_BYTES = 1024 * 1024
DEFAULT_CHECKPOINT_SECONDS = 5.0

_HEX = frozenset(string.hexdigits.lower())
_DIGEST_HEX_LENGTH = 64
_SIGNATURE_HEX_LENGTH = 128  # Ed25519: 64 bytes


def _blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def _hex_text(text: str) -> str:
    """Lowercase hex of the UTF-8 of ``text``; empty for an empty text."""
    return text.encode("utf-8").hex()


def _is_hex(value: str, length: Optional[int] = None) -> bool:
    return (length is None or len(value) == length) and set(value) <= _HEX \
        and len(value) % 2 == 0


# ---------------------------------------------------------------------------------
# The parts of a receipt
# ---------------------------------------------------------------------------------

def execution_name(token: str, peer_id: str) -> str:
    """``blake2b_256(token || peer_id)``, lowercase hex: how a receipt names an execution.

    Decided in #419: ``token`` is the instance token (``local_instances.id``), the
    bearer credential, as the UTF-8 bytes of the string the client got in
    ``ServiceInstance.token``. ``peer_id`` is the node's public identity key as its
    **32 raw bytes**, not its hex text, so the spelling of the hex cannot change the
    name (``normalize_public_key_hex`` gives the one spelling, and ``bytes.fromhex`` of
    it the bytes). No prefix and no length: the key has a fixed length and goes last,
    so the split between the two parts is unambiguous.

    The client knows its token and recomputes the name. A reader who only has the
    receipt sees a digest that does not open the instance.
    """
    key = normalize_public_key_hex(peer_id)
    if key is None:
        raise ValueError(f"Not an identity public key: {peer_id!r}")
    return _blake2b_256(str(token).encode("utf-8") + bytes.fromhex(key)).hex()


def canonical_envs(envs: Mapping[str, bytes], origins: Optional[Mapping[str, str]] = None) -> str:
    """The canonical text of an environment-variable map.

    One entry per variable, sorted by the UTF-8 bytes of its name, joined by ``/``::

        origin "~" hex(UTF-8(name)) "~" hex(value)

    ``value`` is the raw bytes the service got, never a decoded text: the
    ``local_instances.envs`` column decodes with ``errors="replace"``, so two maps can
    give the same column, and a digest over it would attest less than it seems to.
    ``origin`` is ``client`` or ``node`` (default ``client``).
    """
    origins = origins or {}
    entries = []
    for name in sorted(envs, key=lambda key: str(key).encode("utf-8")):
        origin = origins.get(name, ORIGIN_CLIENT)
        if origin not in (ORIGIN_CLIENT, ORIGIN_NODE):
            raise ValueError(f"Unknown origin {origin!r} for {name!r}.")
        entries.append(
            f"{origin}~{_hex_text(str(name))}~{bytes(envs[name]).hex()}"
        )
    return "/".join(entries)


def envs_digest(envs: Mapping[str, bytes], salt: bytes,
                origins: Optional[Mapping[str, str]] = None) -> str:
    """``blake2b_256(salt || UTF-8(canonical_envs))``, lowercase hex.

    The salt keeps a low-entropy value (a flag, a port) from being guessed from the
    digest by whoever reads a published receipt. It travels in the receipt
    (``envs_salt``) but outside what the node signs, so a client can publish the
    signed receipt without it.

    The salt is always :data:`ENVS_SALT_BYTES` long, and a verifier must refuse another
    length (:func:`envs_match`): nothing separates the salt from the text, so a longer
    salt could hold the first entries of the map and open the digest with fewer.
    """
    return _blake2b_256(bytes(salt) + canonical_envs(envs, origins).encode("utf-8")).hex()


def envs_match(receipt: celaut_pb2.ExecutionReceipt, envs: Mapping[str, bytes],
               origins: Optional[Mapping[str, str]] = None,
               salt: Optional[bytes] = None) -> bool:
    """Whether ``envs`` is the map that the ``start`` receipt names.

    ``salt`` defaults to the ``envs_salt`` of the receipt. A salt of another length
    than :data:`ENVS_SALT_BYTES` never matches. This only compares the digest: check
    the signature with :func:`verify`.
    """
    salt = bytes(receipt.envs_salt if salt is None else salt)
    if receipt.kind != KIND_START or len(salt) != ENVS_SALT_BYTES:
        return False
    try:
        return envs_digest(envs, salt, origins) == receipt.envs_digest
    except ValueError:  # an unknown origin
        return False


def chain_link(prev: str, seq: int, direction: str, length: int, payload_digest: str) -> str:
    """The head of a chain after one more event, lowercase hex::

        blake2b_256(UTF-8(prev "|" seq "|" direction "|" length "|" payload_digest))

    ``seq`` counts the events of that chain from 1. ``payload_digest`` is
    ``blake2b_256(payload)`` in hex. A chain starts at :data:`CHAIN_GENESIS`. Dropping,
    adding or moving an event changes every head after it.
    """
    text = f"{prev}|{int(seq)}|{direction}|{int(length)}|{payload_digest}"
    return _blake2b_256(text.encode("utf-8")).hex()


def payload_digest(payload: bytes) -> str:
    return _blake2b_256(bytes(payload)).hex()


def replay_chain(events: Iterable, direction: str) -> Tuple[int, int, str]:
    """``(events, bytes, head)`` of one chain, from what the client saw.

    ``events`` is the payloads in the order they crossed (``bytes``), or
    ``(length, payload)`` pairs where the length differs from the payload (a capture
    cut at its snap length). A tunnel client gets the same messages the node relayed,
    one message per event, so it can compare its heads with a checkpoint's.
    """
    head, count, total = CHAIN_GENESIS, 0, 0
    for event in events:
        length, payload = (len(event), event) if isinstance(event, (bytes, bytearray)) \
            else (int(event[0]), event[1])
        count += 1
        total += length
        head = chain_link(head, count, direction, length, payload_digest(payload))
    return count, total, head


def canonical_scheme(scheme) -> str:
    """The scheme as the Peer digest encodes it (``peer.digest.scheme``)::

        ";".join(sorted(hex(formal) "~" ",".join(sorted(tags)) "~" prose))
    """
    return ";".join(sorted(
        f"{bytes(c.formal).hex()}~{','.join(sorted(c.tags))}~{c.prose}"
        for c in scheme.components
    ))


def receipt_scheme() -> "celaut_pb2.Peer.SignatureScheme":
    """This node's signature scheme without the prose: a receipt carries the tags and
    the ``formal`` that decide, and not a kilobyte of text on every checkpoint."""
    scheme = celaut_pb2.Peer.SignatureScheme()
    for component in SIGNATURE_SCHEME_COMPONENTS:
        scheme.components.add(tags=list(component.tags), formal=component.formal)
    return scheme


def canonical_receipt(receipt: celaut_pb2.ExecutionReceipt) -> str:
    """The text that :func:`receipt_digest` hashes: every field but ``envs_salt`` and
    ``signature``, in field order, joined by ``|``::

        hex(kind) | peer_id | execution_name | ts
        | hex(service_id.type) "~" hex(service_id.value) | envs_digest | hex(initial_mu.n)
        | hex(plane) | session | hex(slot) | seq
        | in_events | in_bytes | in_head | out_events | out_bytes | out_head
        | hex(mu_charged.n) | hex(coverage) | hex(capture_mode) | hex(degraded_reason)
        | blake2b_256(UTF-8(canonical_scheme(signature_scheme)))

    hex() of a text is the lowercase hex of its UTF-8; numbers are decimal; a field
    the kind does not use is empty (``""``, ``0``, an empty ``~``).
    """
    r = receipt
    return "|".join((
        _hex_text(r.kind), r.peer_id, r.execution_name, str(int(r.ts)),
        f"{bytes(r.service_id.type).hex()}~{bytes(r.service_id.value).hex()}",
        r.envs_digest, _hex_text(r.initial_mu.n),
        _hex_text(r.plane), r.session, _hex_text(r.slot), str(int(r.seq)),
        str(int(r.in_events)), str(int(r.in_bytes)), r.in_head,
        str(int(r.out_events)), str(int(r.out_bytes)), r.out_head,
        _hex_text(r.mu_charged.n), _hex_text(r.coverage), _hex_text(r.capture_mode),
        _hex_text(r.degraded_reason),
        _blake2b_256(canonical_scheme(r.signature_scheme).encode("utf-8")).hex(),
    ))


def receipt_digest(receipt: celaut_pb2.ExecutionReceipt) -> str:
    """Blake2b-256 hex of the UTF-8 of :func:`canonical_receipt`."""
    return _blake2b_256(canonical_receipt(receipt).encode("utf-8")).hex()


def signed_bytes(receipt: celaut_pb2.ExecutionReceipt) -> bytes:
    """The exact bytes the signature is over (``receipt.signature.payload``)."""
    return receipt_payload(receipt.peer_id, receipt_digest(receipt)).encode("utf-8")


# ---------------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------------

def sign_with(receipt: celaut_pb2.ExecutionReceipt, private_key: ed25519.Ed25519PrivateKey) -> None:
    """Fill ``peer_id`` and ``signature_scheme`` from ``private_key`` and sign.

    ``execution_name`` must already be set: it needs the token, which only the caller
    has.
    """
    from cryptography.hazmat.primitives import serialization
    receipt.peer_id = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    receipt.signature_scheme.CopyFrom(receipt_scheme())
    receipt.signature = private_key.sign(signed_bytes(receipt)).hex()


# This node's identity, ``(peer_id, private key)``, read once. The identity mnemonic
# does not change while the node runs (``node_identity.get_identity_mnemonic``), and a
# tunnel or an Observe session that asks for receipts must not read the config on its
# open path. None is not kept: a process that reads it before the config is loaded
# reads it again later.
_node_key: Optional[Tuple[str, ed25519.Ed25519PrivateKey]] = None
_node_key_lock = threading.Lock()


def _node_identity_key() -> Optional[Tuple[str, ed25519.Ed25519PrivateKey]]:
    global _node_key
    with _node_key_lock:
        if _node_key is None:
            from src.identity.node_identity import _cached_keypair, get_identity_mnemonic
            mnemonic = get_identity_mnemonic()
            if mnemonic:
                _node_key = _cached_keypair(mnemonic)
        return _node_key


def node_signer() -> Optional[Callable[[celaut_pb2.ExecutionReceipt], None]]:
    """A signer with this node's identity key, or None if the node has none."""
    key = _node_identity_key()
    if key is None:
        return None
    private_key = key[1]
    return lambda receipt: sign_with(receipt, private_key)


def node_peer_id() -> Optional[str]:
    key = _node_identity_key()
    return key[0] if key is not None else None


def node_execution_name(token: str) -> Optional[str]:
    """The execution name of ``token`` on this node, or None if the node has no key.

    The ledger writes it in the cause of the ``start`` and ``tunnel`` movements of the
    instance, so that a reader of a balance statement and of a receipt can see that
    they are about the same execution.
    """
    peer_id = node_peer_id()
    return execution_name(token, peer_id) if peer_id else None


def start_receipt(
        token: str,
        service_id: celaut_pb2.Metadata.HashTag.Hash,
        envs: Mapping[str, bytes],
        initial_mu: int,
        *,
        origins: Optional[Mapping[str, str]] = None,
        signer: Optional[Callable] = None,
        peer_id: Optional[str] = None,
        ts: Optional[int] = None,
        salt: Optional[bytes] = None,
) -> Optional[celaut_pb2.ExecutionReceipt]:
    """The signed ``start`` receipt of an instance, or None with no identity key.

    ``envs`` is the map the service got (``Configuration.environment_variables`` after
    the node took out the reserved ``__nodo_instance_name``, which is a name for the
    instance and not a variable of the service).
    """
    if signer is None:
        signer, peer_id = node_signer(), node_peer_id()
    if signer is None or not peer_id:
        return None
    salt = os.urandom(ENVS_SALT_BYTES) if salt is None else bytes(salt)
    receipt = celaut_pb2.ExecutionReceipt(
        kind=KIND_START,
        execution_name=execution_name(token, peer_id),
        ts=int(time.time()) if ts is None else int(ts),
        service_id=service_id,
        envs_digest=envs_digest(envs, salt, origins),
        envs_salt=salt,
        initial_mu=celaut_pb2.Amount(n=str(int(initial_mu))),
    )
    signer(receipt)
    return receipt


# ---------------------------------------------------------------------------------
# The chains of a stream
# ---------------------------------------------------------------------------------

_intervals: Optional[Tuple[int, float]] = None


def checkpoint_intervals() -> Tuple[int, float]:
    """``(bytes, seconds)`` from ``receipts.CHECKPOINT_BYTES`` / ``CHECKPOINT_SECONDS``.

    Read once per process, as the config itself is (``ConfigManager.ensure_loaded``).
    Anything unreadable or not positive takes the default: the intervals are a policy,
    and a bad value must not stop a tunnel.
    """
    global _intervals
    if _intervals is not None:
        return _intervals
    every_bytes, every_seconds = DEFAULT_CHECKPOINT_BYTES, DEFAULT_CHECKPOINT_SECONDS
    try:
        from src.utils.config import ConfigManager
        config = ConfigManager()
        every_bytes = int(config.get("receipts.CHECKPOINT_BYTES", every_bytes))
        every_seconds = float(config.get("receipts.CHECKPOINT_SECONDS", every_seconds))
    except Exception:
        pass
    _intervals = (
        every_bytes if every_bytes > 0 else DEFAULT_CHECKPOINT_BYTES,
        every_seconds if every_seconds > 0 else DEFAULT_CHECKPOINT_SECONDS,
    )
    return _intervals


class ReceiptChain:
    """The two hash chains and the checkpoints of one stream.

    ``record`` is called after a successful ``TrafficMeter.add`` from both threads of a
    tunnel (or from the Observe loop), so it holds a lock. ``checkpoint`` is called by
    the thread that writes the response, when :meth:`due` says so, and once at the end
    with ``final=True`` for the ``close``. Nothing is sent after the close.

    ``checkpoint`` signs first and changes the state after. If the signer fails, the
    state stays as it was (the ``seq`` is not used, and a failed ``close`` does not close
    the chain), the stream goes on with no receipt, and the next try waits one
    ``every_seconds``.

    Each direction has its own chain and its own event count, so a client can rebuild
    each one from the messages it sent and got (:func:`replay_chain`): the order in which
    the node saw the two directions interleave is not something the client can know.
    """

    def __init__(
            self,
            token: str,
            plane: str,
            coverage: str,
            *,
            slot: str = "",
            capture_mode: str = "",
            degraded_reason: str = "",
            signer: Callable,
            peer_id: str,
            every_bytes: int = DEFAULT_CHECKPOINT_BYTES,
            every_seconds: float = DEFAULT_CHECKPOINT_SECONDS,
            clock: Callable[[], float] = time.time,
            monotonic: Callable[[], float] = time.monotonic,
            session: Optional[str] = None,
    ) -> None:
        self.name = execution_name(token, peer_id)
        self.plane = plane
        self.coverage = coverage
        self.slot = slot
        self.capture_mode = capture_mode
        self.degraded_reason = degraded_reason
        self.session = session or os.urandom(SESSION_BYTES).hex()
        self._signer = signer
        self._every_bytes = every_bytes
        self._every_seconds = every_seconds
        self._clock = clock
        self._monotonic = monotonic

        self._lock = threading.Lock()
        self._chains: Dict[str, List] = {d: [0, 0, CHAIN_GENESIS] for d in DIRECTIONS}
        self._seq = 0
        self._closed = False
        self._pending_events = 0
        self._pending_bytes = 0
        self._last_checkpoint = monotonic()
        self._retry_at = 0.0

    @classmethod
    def for_node(cls, token: str, plane: str, coverage: str, **kwargs) -> Optional["ReceiptChain"]:
        """A chain signed with this node's identity key, or None if it has none."""
        signer, peer_id = node_signer(), node_peer_id()
        if signer is None or not peer_id:
            return None
        every_bytes, every_seconds = checkpoint_intervals()
        return cls(token, plane, coverage, signer=signer, peer_id=peer_id,
                   every_bytes=every_bytes, every_seconds=every_seconds, **kwargs)

    def record(self, direction: str, payload: bytes, length: Optional[int] = None) -> None:
        """Add one event to the chain of ``direction``. Hashing is outside the lock."""
        digest = payload_digest(payload)
        length = len(payload) if length is None else int(length)
        with self._lock:
            if self._closed:
                return
            chain = self._chains[direction]
            chain[0] += 1
            chain[1] += length
            chain[2] = chain_link(chain[2], chain[0], direction, length, digest)
            self._pending_events += 1
            self._pending_bytes += length

    def due(self) -> bool:
        with self._lock:
            if self._closed or not self._pending_events:
                return False
            now = self._monotonic()
            if now < self._retry_at:
                return False
            return self._pending_bytes >= self._every_bytes or \
                now - self._last_checkpoint >= self._every_seconds

    def checkpoint(self, *, mu_charged: Optional[int] = None,
                   final: bool = False) -> Optional[celaut_pb2.ExecutionReceipt]:
        """The next signed checkpoint, or the ``close`` with ``final``.

        None once closed, or when the signer fails: then nothing changes (see the class).
        The signature is made under the lock, so the counters that it covers are the
        ones that it resets. It takes tens of microseconds.
        """
        with self._lock:
            if self._closed:
                return None
            (in_events, in_bytes, in_head), (out_events, out_bytes, out_head) = (
                tuple(self._chains[DIRECTION_IN]), tuple(self._chains[DIRECTION_OUT])
            )
            receipt = celaut_pb2.ExecutionReceipt(
                kind=KIND_CLOSE if final else KIND_CHECKPOINT,
                execution_name=self.name,
                ts=int(self._clock()),
                plane=self.plane,
                session=self.session,
                slot=self.slot,
                seq=self._seq + 1,
                in_events=in_events, in_bytes=in_bytes, in_head=in_head,
                out_events=out_events, out_bytes=out_bytes, out_head=out_head,
                coverage=self.coverage,
                capture_mode=self.capture_mode,
                degraded_reason=self.degraded_reason,
            )
            if mu_charged is not None:
                receipt.mu_charged.n = str(int(mu_charged))
            try:
                self._signer(receipt)
            except Exception as e:
                self._retry_at = self._monotonic() + self._every_seconds
                from src.utils.logger import LOGGER
                LOGGER(f"[receipts] Cannot sign {receipt.kind} {receipt.seq} of stream "
                       f"{self.session}: {e}. The stream goes on with no receipt.")
                return None
            self._seq += 1
            self._closed = final
            self._pending_events = self._pending_bytes = 0
            self._last_checkpoint = self._monotonic()
        return receipt


# ---------------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------------

def _scheme_is_ours(scheme) -> bool:
    ours = list(receipt_scheme().components)
    theirs = list(scheme.components)
    return len(theirs) == len(ours) and all(
        any(same_component(t, o) for o in ours) for t in theirs
    )


# The signed fields that only some kinds or planes use. A field that a receipt does not
# use must be empty or zero (docs/EXECUTION_RECEIPTS.md), and problems() checks it:
# a value there is signed, so a reader could take it for a claim.
_START_FIELDS = ("service_id", "envs_digest", "initial_mu")
_STREAM_FIELDS = ("plane", "session", "slot", "seq", "in_events", "in_bytes", "in_head",
                  "out_events", "out_bytes", "out_head", "mu_charged", "coverage",
                  "capture_mode", "degraded_reason")
_TUNNEL_FIELDS = ("slot", "mu_charged")
_OBSERVE_FIELDS = ("capture_mode", "degraded_reason")


def _is_set(receipt: celaut_pb2.ExecutionReceipt, field: str) -> bool:
    """Whether ``field`` adds anything to the canonical text: a text, a number, or a
    message (service_id, an Amount) with a value in it."""
    value = getattr(receipt, field)
    if field == "service_id":
        return bool(bytes(value.type) or bytes(value.value))
    if field in ("initial_mu", "mu_charged"):
        return bool(value.n)
    return bool(value)


def problems(receipt: celaut_pb2.ExecutionReceipt, *, peer_id: Optional[str] = None,
             token: Optional[str] = None) -> List[str]:
    """Why ``receipt`` is not a valid receipt; an empty list when it is.

    ``peer_id``, if given, is the node the receipt must be from. ``token``, if given,
    is the token the client holds: the name must be the one it gives. The shape checks
    are what make the canonical text have one reading, so they come before the
    signature.
    """
    found: List[str] = []
    r = receipt
    if r.kind not in KINDS:
        found.append(f"kind {r.kind!r} is not one of {', '.join(KINDS)}")
    if normalize_public_key_hex(r.peer_id) != r.peer_id or not r.peer_id:
        found.append("peer_id is not a lowercase 64-hex public key")
    if not _is_hex(r.execution_name, _DIGEST_HEX_LENGTH):
        found.append("execution_name is not a 64-hex digest")
    for field in ("envs_digest", "in_head", "out_head"):
        value = getattr(r, field)
        if value and not _is_hex(value, _DIGEST_HEX_LENGTH):
            found.append(f"{field} is not a 64-hex digest")
    if r.session and not _is_hex(r.session):
        found.append("session is not lowercase hex")
    # bytes.fromhex reads uppercase and spaces too: one spelling only, as the proto says.
    if not _is_hex(r.signature, _SIGNATURE_HEX_LENGTH):
        found.append("signature is not 128 lowercase hex")
    if r.envs_salt and (r.kind != KIND_START or len(r.envs_salt) != ENVS_SALT_BYTES):
        found.append(f"envs_salt is not {ENVS_SALT_BYTES} bytes of a start receipt")

    if r.kind == KIND_START:
        if not bytes(r.service_id.type) or not bytes(r.service_id.value) \
                or not r.envs_digest or not r.initial_mu.n:
            found.append("a start receipt needs service_id, envs_digest and initial_mu")
        used = [f for f in _STREAM_FIELDS if _is_set(r, f)]
        if used:
            found.append(f"a start receipt has no stream fields (set: {', '.join(used)})")
    elif r.kind in (KIND_CHECKPOINT, KIND_CLOSE):
        if r.plane not in PLANES:
            found.append(f"plane {r.plane!r} is not one of {', '.join(PLANES)}")
        elif r.coverage not in PLANE_COVERAGES[r.plane]:
            found.append(f"coverage {r.coverage!r} is not one of "
                         f"{', '.join(PLANE_COVERAGES[r.plane])} for plane {r.plane}")
        if not r.session or r.seq < 1 or not r.in_head or not r.out_head:
            found.append("a checkpoint needs session, seq >= 1, in_head and out_head")
        used = [f for f in _START_FIELDS if _is_set(r, f)]
        if used:
            found.append(f"a checkpoint has no start fields (set: {', '.join(used)})")
        if r.plane == PLANE_TUNNEL:
            if not r.slot or not r.mu_charged.n:
                found.append("a tunnel checkpoint needs slot and mu_charged")
            used = [f for f in _OBSERVE_FIELDS if _is_set(r, f)]
            if used:
                found.append(f"a tunnel checkpoint has no capture fields (set: {', '.join(used)})")
        elif r.plane == PLANE_OBSERVE:
            if not r.capture_mode:
                found.append("an observe checkpoint needs capture_mode")
            elif r.coverage == COVERAGE_TAP and r.capture_mode != "pcap":
                found.append(f"coverage {COVERAGE_TAP} needs capture_mode pcap")
            used = [f for f in _TUNNEL_FIELDS if _is_set(r, f)]
            if used:
                found.append(f"an observe checkpoint has no tunnel fields (set: {', '.join(used)})")

    if not _scheme_is_ours(r.signature_scheme):
        found.append("signature_scheme is not the ed25519 scheme this code verifies")
    if peer_id is not None and r.peer_id != normalize_public_key_hex(peer_id):
        found.append("peer_id is not the expected node")
    if token is not None and not found:
        if r.execution_name != execution_name(token, r.peer_id):
            found.append("execution_name is not the name of this token on this node")

    if not found:
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(r.peer_id)).verify(
                bytes.fromhex(r.signature), signed_bytes(r)
            )
        except (InvalidSignature, ValueError, TypeError):
            found.append("the signature does not verify")
    return found


def verify(receipt: celaut_pb2.ExecutionReceipt, *, peer_id: Optional[str] = None,
           token: Optional[str] = None) -> bool:
    """Whether ``receipt`` is well formed and signed by its ``peer_id``."""
    return not problems(receipt, peer_id=peer_id, token=token)


def check_sequence(receipts: Iterable[celaut_pb2.ExecutionReceipt], *,
                   peer_id: Optional[str] = None, token: Optional[str] = None) -> Dict:
    """Check a set of receipts of one execution, as a client holds them.

    Returns a report: each receipt's problems, then across them:

    * ``contradictions``: two valid receipts for the same thing (the start of a name,
      or one ``seq`` of one stream) with different content. Signed by one key, this is
      the node proving that it lies (case 1 of the doc), and needs nothing else.
    * ``errors``: a valid set cannot have them -- receipts of different nodes or
      names, a counter or the charge going down, a checkpoint after the close.
    * ``gaps``: a missing seq. Not an error -- a client may keep only some -- but
      the highest seq is what a dispute reads.
    * ``streams``: per stream, the last seq and whether a close was seen.
    """
    receipts = list(receipts)
    report: Dict = {"receipts": [], "contradictions": [], "errors": [], "gaps": [],
                    "streams": {}, "valid": True}
    valid: List[celaut_pb2.ExecutionReceipt] = []
    for index, receipt in enumerate(receipts):
        found = problems(receipt, peer_id=peer_id, token=token)
        report["receipts"].append({
            "index": index, "kind": receipt.kind, "seq": int(receipt.seq),
            "session": receipt.session, "valid": not found, "problems": found,
        })
        if not found:
            valid.append(receipt)

    if len({r.peer_id for r in valid}) > 1:
        report["errors"].append("the receipts are signed by more than one node")
    if len({r.execution_name for r in valid}) > 1:
        report["errors"].append("the receipts name more than one execution")

    seen: Dict[Tuple, str] = {}
    for r in valid:
        key = (r.peer_id, r.execution_name, r.kind == KIND_START, r.session, int(r.seq))
        digest = receipt_digest(r)
        if key in seen and seen[key] != digest:
            report["contradictions"].append({
                "execution_name": r.execution_name, "session": r.session,
                "seq": int(r.seq), "kind": r.kind,
                "digests": sorted({seen[key], digest}),
            })
        seen.setdefault(key, digest)

    sessions: Dict[str, List[celaut_pb2.ExecutionReceipt]] = {}
    for r in valid:
        if r.kind != KIND_START:
            sessions.setdefault(r.session, []).append(r)
    for session, items in sessions.items():
        items.sort(key=lambda r: int(r.seq))
        closes = [r for r in items if r.kind == KIND_CLOSE]
        last_seq = int(items[-1].seq)
        if closes and int(closes[0].seq) != last_seq:
            report["errors"].append(f"stream {session}: a checkpoint after its close")
        if len({(r.plane, r.slot, r.coverage) for r in items}) > 1:
            report["errors"].append(f"stream {session}: plane, slot or coverage changes")
        expected = 1
        previous = None
        for r in items:
            if int(r.seq) > expected:
                report["gaps"].append({"session": session, "from": expected,
                                       "to": int(r.seq) - 1})
            expected = int(r.seq) + 1
            if previous is not None and int(previous.seq) != int(r.seq):
                counters = ("in_events", "in_bytes", "out_events", "out_bytes")
                if any(getattr(r, c) < getattr(previous, c) for c in counters):
                    report["errors"].append(
                        f"stream {session}: a counter goes down at seq {int(r.seq)}")
                if int(r.mu_charged.n or 0) < int(previous.mu_charged.n or 0):
                    report["errors"].append(
                        f"stream {session}: the charge goes down at seq {int(r.seq)}")
                if (r.in_events == previous.in_events and r.in_head != previous.in_head) or \
                        (r.out_events == previous.out_events and r.out_head != previous.out_head):
                    report["errors"].append(
                        f"stream {session}: a head changes with no new event at seq {int(r.seq)}")
            previous = r
        report["streams"][session] = {
            "plane": items[-1].plane, "slot": items[-1].slot,
            "coverage": items[-1].coverage, "last_seq": last_seq,
            "closed": bool(closes),
            "in_events": int(items[-1].in_events), "in_bytes": int(items[-1].in_bytes),
            "out_events": int(items[-1].out_events), "out_bytes": int(items[-1].out_bytes),
            "mu_charged": items[-1].mu_charged.n,
        }

    report["valid"] = bool(receipts) and all(e["valid"] for e in report["receipts"]) \
        and not report["errors"] and not report["contradictions"]
    return report


# ---------------------------------------------------------------------------------
# JSON, for files a client keeps and the CLI reads
# ---------------------------------------------------------------------------------

def to_json_dict(receipt: celaut_pb2.ExecutionReceipt) -> Dict:
    """The proto3 JSON mapping with the field names of celaut.proto (bytes in base64,
    64-bit integers as strings), which any protobuf library reads back."""
    from google.protobuf import json_format
    return json_format.MessageToDict(receipt, preserving_proto_field_name=True)


def from_json_dict(document: Dict) -> celaut_pb2.ExecutionReceipt:
    from google.protobuf import json_format
    return json_format.ParseDict(document, celaut_pb2.ExecutionReceipt())
