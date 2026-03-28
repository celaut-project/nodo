import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Final, Iterable, Optional

from src.utils.config import ConfigManager

SHA256_ID: Final[bytes] = bytes.fromhex(
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
SHA3_256_ID: Final[bytes] = bytes.fromhex(
    "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"
)
SHAKE_256_ID: Final[bytes] = bytes.fromhex(
    "46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f"
)
BLAKE2B_ID: Final[bytes] = bytes.fromhex(
    "0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8"
)

SHA256: Callable[[bytes], bytes] = (
    lambda value: b"" if value is None else hashlib.sha256(value).digest()
)
SHA3_256: Callable[[bytes], bytes] = (
    lambda value: b"" if value is None else hashlib.sha3_256(value).digest()
)
SHAKE_256: Callable[[bytes], bytes] = (
    lambda value: b"" if value is None else hashlib.shake_256(value).digest(32)
)
BLAKE2B: Callable[[bytes], bytes] = (
    lambda value: b"" if value is None else hashlib.blake2b(value, digest_size=32).digest()
)

HASH_FUNCTIONS: Final[Dict[bytes, Callable[[bytes], bytes]]] = {
    SHA256_ID: SHA256,
    SHA3_256_ID: SHA3_256,
    SHAKE_256_ID: SHAKE_256,
    BLAKE2B_ID: BLAKE2B,
}

DEFAULT_HASH_NAME: Final[str] = "sha3_256"


@dataclass(frozen=True)
class HashSpec:
    name: str
    id_bytes: bytes
    digest_size: int
    hasher_factory: Callable[[], "hashlib._Hash"]
    finalize: Callable[["hashlib._Hash", int], bytes]


def _finalize_digest(hasher: "hashlib._Hash", _: int) -> bytes:
    return hasher.digest()


def _finalize_shake(hasher: "hashlib._Hash", digest_size: int) -> bytes:
    return hasher.digest(digest_size)


HASH_SPECS: Final[Dict[bytes, HashSpec]] = {
    SHA256_ID: HashSpec(
        name="sha256",
        id_bytes=SHA256_ID,
        digest_size=32,
        hasher_factory=hashlib.sha256,
        finalize=_finalize_digest,
    ),
    SHA3_256_ID: HashSpec(
        name="sha3_256",
        id_bytes=SHA3_256_ID,
        digest_size=32,
        hasher_factory=hashlib.sha3_256,
        finalize=_finalize_digest,
    ),
    SHAKE_256_ID: HashSpec(
        name="shake_256",
        id_bytes=SHAKE_256_ID,
        digest_size=32,
        hasher_factory=hashlib.shake_256,
        finalize=_finalize_shake,
    ),
    BLAKE2B_ID: HashSpec(
        name="blake2b",
        id_bytes=BLAKE2B_ID,
        digest_size=32,
        hasher_factory=lambda: hashlib.blake2b(digest_size=32),
        finalize=_finalize_digest,
    ),
}

HASH_NAME_TO_ID: Final[Dict[str, bytes]] = {
    "sha256": SHA256_ID,
    "sha2_256": SHA256_ID,
    "sha3": SHA3_256_ID,
    "sha3_256": SHA3_256_ID,
    "shake": SHAKE_256_ID,
    "shake_256": SHAKE_256_ID,
    "blake2": BLAKE2B_ID,
    "blake2b": BLAKE2B_ID,
}

HASH_ID_TO_NAME: Final[Dict[bytes, str]] = {
    spec.id_bytes: spec.name for spec in HASH_SPECS.values()
}


def resolve_hash_config(value: str) -> HashSpec:
    normalized = str(value or "").strip().lower()
    if not normalized:
        normalized = DEFAULT_HASH_NAME

    if normalized in HASH_NAME_TO_ID:
        return HASH_SPECS[HASH_NAME_TO_ID[normalized]]

    try:
        hash_id = bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Invalid hash selector '{value}'. Use a known name or a hash id in hex."
        ) from exc

    if hash_id not in HASH_SPECS:
        supported_names = ", ".join(sorted(set(HASH_NAME_TO_ID.keys())))
        supported_ids = ", ".join(spec.id_bytes.hex() for spec in HASH_SPECS.values())
        raise ValueError(
            f"Unsupported hash selector '{value}'. Supported names: {supported_names}. "
            f"Supported ids: {supported_ids}"
        )
    return HASH_SPECS[hash_id]


def get_configured_hash_spec(config_manager: Optional[ConfigManager] = None) -> HashSpec:
    manager = config_manager or ConfigManager()
    configured_value = manager.get("hashing.HASH", DEFAULT_HASH_NAME)
    return resolve_hash_config(str(configured_value))


def get_configured_hash_id(config_manager: Optional[ConfigManager] = None) -> bytes:
    return get_configured_hash_spec(config_manager).id_bytes


def hash_stream(chunks_iter: Iterable[bytes], spec: HashSpec) -> bytes:
    hasher = spec.hasher_factory()
    for chunk in chunks_iter:
        hasher.update(chunk)
    return spec.finalize(hasher, spec.digest_size)


def hash_bytes(value: bytes, spec: HashSpec) -> bytes:
    return hash_stream([value], spec)


def hash_file(path: Path, spec: HashSpec, chunk_size: int = 1 << 20) -> bytes:
    def _iter_file():
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(chunk_size), b""):
                yield block

    return hash_stream(_iter_file(), spec)
