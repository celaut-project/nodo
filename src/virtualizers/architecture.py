from typing import Optional, Set
from protos import celaut_pb2
from src.utils.architectures import SUPPORTED_ARCHITECTURES
from src.utils.config import ConfigManager

# Load environment configuration
env_manager = ConfigManager()
TRUST_METADATA_ARCHITECTURE = env_manager.get("TRUST_METADATA_ARCHITECTURE")

# Build a mapping from architecture aliases to their canonical form (first element of each list)
_ARCH_CANONICAL = {
    alias: arch_list[0]
    for arch_list in SUPPORTED_ARCHITECTURES
    for alias in arch_list
}


def _tags_from_metadata(metadata: celaut_pb2.Metadata) -> Set[str]:
    """
    Safely extract a set of tags from metadata.hashtag.attr_hashtag.
    """
    try:
        groups = metadata.hashtag.attr_hashtag
        attrs = groups[1][0].attr_hashtag
        return { tag for ah in attrs for tag in ah.tag }
    except (IndexError, AttributeError):  # Handle unexpected structure
        return set()


def get_arch_tag(
    service: celaut_pb2.Service,
    metadata: Optional[celaut_pb2.Metadata]
) -> Optional[str]:
    """
    Returns the supported architecture (canonical form) found in Service or,
    in debug mode, in Metadata. Returns None if none is found.
    """
    # 1) Check in Service
    for tag in service.container.architecture.tags:
        if tag in _ARCH_CANONICAL:
            return _ARCH_CANONICAL[tag]

    # 2) Check in Metadata (only for debug)
    if TRUST_METADATA_ARCHITECTURE and metadata:
        meta_tags = _tags_from_metadata(metadata)
        for tag in meta_tags:
            if tag in _ARCH_CANONICAL:
                return _ARCH_CANONICAL[tag]

    return None


def check_supported_architecture(
    service: celaut_pb2.Service,
    metadata: Optional[celaut_pb2.Metadata]
) -> bool:
    """
    Returns True if at least one supported architecture is found.
    """
    return get_arch_tag(service, metadata) is not None


class UnsupportedArchitectureException(Exception):
    """
    Exception raised when the architecture is not supported.
    """
    def __init__(self, arch: Optional[str]):
        canonical_list = [lst[0] for lst in SUPPORTED_ARCHITECTURES]
        self.message = (
            f"Unsupported architecture '{arch}'.\n"
            f"Supported architectures: {canonical_list}."
        )
        super().__init__(self.message)
