"""The proof of work that stands between a stranger and a new client (issue #361).

``GenerateClient`` takes no authentication -- it cannot, since it is how a caller gets
its first identity on this node -- so anybody who can reach the gateway can ask for as
many clients as they like, and each one costs a row and a free-tier credit. This module
is the price attached to that, once the node has already given away more than it meant
to: the first ``free_tier.MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY`` clients are free, and
every block of that many raises the difficulty by one.

Three properties shape everything here:

**Stateless.** The node stores nothing about a challenge it issued -- no pending nonce,
no reserved ``client_id``, no expiry. Everything a validation needs travels in the
challenge, and an HMAC over it is what makes that safe: the caller holds the challenge
and cannot change a field of it. That matters precisely because this is the DoS defence:
a defence that allocated a row per unauthenticated request would be the attack.

**The ``client_id`` comes from the caller**, as a UUID4, and is authenticated inside the
challenge. So the node can answer "you already have that one" from a single indexed
lookup *before* hashing anything, and a solved challenge cannot be replayed to create
the same client twice -- the second attempt is refused at that lookup.

**The difficulty is the one in the challenge**, never the current global one. Difficulty
rises as clients are created, so a caller that started work at 2 and finished after the
node moved to 3 did the work it was asked for; charging it the new price would make a
valid challenge expire by nothing but other people's traffic.

The secret backing the HMAC is derived from ``identity.MNEMONIC``, the node's own
identity seed, through a personalised Blake2b -- so it survives a restart (in-flight
challenges stay valid across one) and is unrelated to the Ed25519 key derived from the
same mnemonic, which is what the personalisation buys. A node with no identity yet
(config never loaded) falls back to 32 random bytes held in memory for the life of the
process: still sound, only forgetful, and the case does not arise on a running node.
"""
import hashlib
import hmac
import os
import secrets
import string
from functools import lru_cache
from typing import Final, Optional, Tuple

from src.identity.node_identity import get_identity_mnemonic

# The one proof of work this node offers. Static, identical for every challenge: they
# describe the rule, and only `challenge` and `difficulty` in a `PoWRequired` vary.
POW_TAGS: Final = ["blake2b"]
POW_PROSE: Final[str] = (
    "Find a solution such that Blake2b(challenge + solution) ends with N zero "
    "characters, where N is the difficulty."
)
POW_FORMAL: Final[bytes] = b'Blake2b(challenge || solution).hexdigest().endswith("0" * difficulty)'

# How many clients this node hands out per difficulty level, when nothing is configured.
DEFAULT_MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY: Final[int] = 500

# Separates the four fields of a challenge. None of them can contain it: the id and the
# nonce are hex, the difficulty is decimal, the mac is hex.
_FIELD_SEPARATOR: Final[str] = ":"

# 16 bytes, the size used for a salt that only has to be unique.
_NONCE_BYTES: Final[int] = 16

# Keeps this key unrelated to the Ed25519 identity key derived from the same mnemonic.
_SECRET_PERSONALISATION: Final[bytes] = b"celaut-gc-pow"

_HEX_DIGITS: Final = frozenset(string.hexdigits.lower())
_UUID4_HEX_LENGTH: Final[int] = 32

# Bounds a difficulty read off the wire. A challenge is authenticated, so this cannot be
# reached by a caller inventing one; it stops a corrupted or absurd value turning a
# validation into an endless string comparison, and caps what `solve_pow` will attempt.
MAX_DIFFICULTY: Final[int] = 64


class PoWError(Exception):
    """A challenge or a solution this node will not accept."""


def is_uuid4_hex(client_id: str) -> bool:
    """True for the 32 lowercase hex characters of a ``uuid4().hex``.

    The id is chosen by the caller and then used as a primary key, so it is checked
    before it reaches the database rather than after.
    """
    candidate = str(client_id or "")
    return len(candidate) == _UUID4_HEX_LENGTH and set(candidate) <= _HEX_DIGITS


def current_difficulty(existing_clients: int, per_difficulty: int) -> int:
    """How much work the next client costs, given how many this node already has.

    One step per ``per_difficulty`` clients: 0 for the first block, 1 for the second,
    and so on without a ceiling -- there is no number of clients at which the node stops
    charging more. A non-positive ``per_difficulty`` would divide by zero (or run
    backwards), so it is refused rather than silently treated as "free forever".
    """
    if per_difficulty <= 0:
        raise ValueError(
            "MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY must be positive, got "
            f"{per_difficulty}. It is the size of a difficulty step, so 0 has no meaning."
        )
    return max(0, int(existing_clients)) // per_difficulty


@lru_cache(maxsize=1)
def _process_secret() -> bytes:
    """The fallback secret, for a node that has no identity mnemonic to derive from."""
    return secrets.token_bytes(32)


def server_secret() -> bytes:
    """The key the challenge MAC is computed under. Never leaves this node.

    Derived from the node's identity mnemonic so that a restart does not invalidate
    every challenge a caller is currently solving. It is not a per-client secret and it
    is not stored beside a client: the same key authenticates every challenge, which is
    all an integrity tag needs.
    """
    mnemonic = get_identity_mnemonic()
    if not mnemonic:
        return _process_secret()
    return hashlib.blake2b(
        mnemonic.encode("utf-8"), key=_SECRET_PERSONALISATION, digest_size=32
    ).digest()


def _mac(client_id: str, nonce: str, difficulty: int, secret: bytes) -> str:
    """HMAC-SHA256 over the three authenticated fields, in a fixed order."""
    material = f"{client_id}{_FIELD_SEPARATOR}{nonce}{_FIELD_SEPARATOR}{difficulty}"
    return hmac.new(secret, material.encode("utf-8"), hashlib.sha256).hexdigest()


def make_challenge(client_id: str, difficulty: int, secret: bytes,
                   nonce: Optional[str] = None) -> str:
    """``client_id:nonce:difficulty:mac``.

    Deterministic given the nonce, so the node re-derives the MAC on the retry instead
    of remembering it. ``nonce`` is only ever passed in by a test; a caller of this on
    the serving path gets a fresh random one, which is what keeps two challenges for the
    same id and difficulty from being the same string.
    """
    if not is_uuid4_hex(client_id):
        raise PoWError("client_id must be the 32 hex characters of a UUID4.")
    if difficulty < 0 or difficulty > MAX_DIFFICULTY:
        raise PoWError(f"difficulty out of range: {difficulty}")
    nonce = nonce if nonce is not None else os.urandom(_NONCE_BYTES).hex()
    return _FIELD_SEPARATOR.join(
        (client_id, nonce, str(difficulty), _mac(client_id, nonce, difficulty, secret))
    )


def parse_and_verify_challenge(challenge: str, secret: bytes) -> Tuple[str, str, int]:
    """Unpack a challenge this node issued, or raise.

    Returns ``(client_id, nonce, difficulty)``, and nothing in the challenge may be
    trusted until it has come back through here: the MAC is what says the node itself
    wrote those three values. Raises `PoWError` on anything else -- a wrong shape, a
    difficulty that is not a number, a MAC that does not match.
    """
    parts = str(challenge or "").split(_FIELD_SEPARATOR)
    if len(parts) != 4:
        raise PoWError("Malformed challenge.")
    client_id, nonce, raw_difficulty, mac = parts

    try:
        difficulty = int(raw_difficulty)
    except ValueError:
        raise PoWError("Malformed challenge: difficulty is not an integer.")

    # Recomputing the MAC over the *received* fields is the whole check: it fails for a
    # changed id, a changed nonce and a changed difficulty alike, because all three are
    # in the material. compare_digest because this runs for anonymous callers.
    expected = _mac(client_id, nonce, difficulty, secret)
    if not hmac.compare_digest(expected, mac):
        raise PoWError("Invalid challenge MAC.")

    # After the MAC, so a caller cannot tell a rejected-for-shape challenge from a
    # forged one by which complaint it gets. These hold for anything this node issued.
    if not is_uuid4_hex(client_id) or difficulty < 0 or difficulty > MAX_DIFFICULTY:
        raise PoWError("Invalid challenge contents.")

    return client_id, nonce, difficulty


def pow_digest(challenge: str, solution: str) -> str:
    """``Blake2b(challenge || solution).hexdigest()`` -- the rule `POW_FORMAL` states."""
    return hashlib.blake2b(
        challenge.encode("utf-8") + str(solution or "").encode("utf-8")
    ).hexdigest()


def verify_solution(challenge: str, solution: str, difficulty: int) -> bool:
    """Whether ``solution`` answers ``challenge`` at ``difficulty``.

    ``difficulty`` is the caller's job to take from the challenge, not from the node's
    current global one -- see this module's docstring. Difficulty 0 accepts anything,
    including no solution at all, which is the "no proof of work required" case falling
    out of the same rule rather than being a second path.
    """
    if difficulty <= 0:
        return True
    return pow_digest(challenge, solution).endswith("0" * difficulty)


def solve_pow(challenge: str, difficulty: int) -> str:
    """Do the work: the smallest counter whose digest ends in ``difficulty`` zeros.

    Counting from 0 rather than searching randomly, so a solution is reproducible and a
    test is not at the mercy of chance. Expected cost is 16**difficulty hashes.
    """
    if difficulty <= 0:
        return ""
    if difficulty > MAX_DIFFICULTY:
        raise PoWError(f"Refusing to solve a difficulty of {difficulty}.")
    suffix = "0" * difficulty
    nonce = 0
    while True:
        solution = str(nonce)
        if pow_digest(challenge, solution).endswith(suffix):
            return solution
        nonce += 1
