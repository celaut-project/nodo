import itertools
from protos import celaut_pb2
from src.utils.env import SUPPORTED_ARCHITECTURES, EnvManager

env_manager = EnvManager()
TRUST_METADATA_ARCHITECTURE = env_manager.get_env("TRUST_METADATA_ARCHITECTURE")


def get_arch_tag(service: celaut_pb2.Service, metadata: celaut_pb2.Metadata) -> str:
    if service.container.architecture.tags:
        for tag in service.container.architecture.tags:
            for _l in SUPPORTED_ARCHITECTURES:
                if tag in _l:
                    return tag
    
    if TRUST_METADATA_ARCHITECTURE:
        for _l in SUPPORTED_ARCHITECTURES:
            if any(a in _l for a in {
                ah.key: ah.value for ah in {
                    ah.key: ah.value for ah in
                    metadata.hashtag.attr_hashtag
                }[1][0].attr_hashtag
            }[1][0].tag):
                return _l[0]


def check_supported_architecture(service: celaut_pb2.Service, metadata: celaut_pb2.Metadata) -> bool:
    if service.container.architecture.tags:
        if any(tag in list(itertools.chain.from_iterable(SUPPORTED_ARCHITECTURES)) 
               for tag in service.container.architecture.tags):
            return True
    
    # In case that the architecture is not on the service architecture, check on metadata.
    if TRUST_METADATA_ARCHITECTURE:
        try:
            return any(a in list(itertools.chain.from_iterable(SUPPORTED_ARCHITECTURES)) for a in
                    {ah.key: ah.value for ah in
                        {ah.key: ah.value for ah in metadata.hashtag.attr_hashtag}[1][0].attr_hashtag}[1][0].tag)
        except Exception:
            return False

class UnsupportedArchitectureException(Exception):

    def __init__(self, arch):
        self.message = f"\n Unsupported architecture {arch} - \n Only these {SUPPORTED_ARCHITECTURES}. \n"

    def __str__(self):
        return self.message