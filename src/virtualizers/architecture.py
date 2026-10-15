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


# AttrHashTag.key is the field number of the part in the message that contains it.
_SERVICE_CONTAINER_FIELD = celaut_pb2.Service.DESCRIPTOR.fields_by_name["container"].number
_CONTAINER_ARCHITECTURE_FIELD = (
    celaut_pb2.Service.Container.DESCRIPTOR.fields_by_name["architecture"].number
)


def _parts(hashtags, key: int):
    """The HashTags of the part at field ``key``, in any of ``hashtags``."""
    return [
        part
        for hashtag in hashtags
        for attr in hashtag.attr_hashtag if attr.key == key
        for part in attr.value
    ]


def _tags_from_metadata(metadata: celaut_pb2.Metadata) -> Set[str]:
    """The tags that the metadata gives for Service.container.architecture.

    Found by the field numbers in ``AttrHashTag.key``, not by position in the list.
    """
    containers = _parts([metadata.hashtag], _SERVICE_CONTAINER_FIELD)
    architectures = _parts(containers, _CONTAINER_ARCHITECTURE_FIELD)
    return {tag for hashtag in architectures for tag in hashtag.tag}


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
