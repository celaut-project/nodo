"""
Supported-architecture tables, derived from config.

These lists used to live in src/utils/runtime.py, which is Docker-specific and
imports the `docker` Python library. They are needed on the Cloud-Hypervisor
execution path (via src/virtualizers/architecture.py), so they live here in a
Docker-free module instead — nothing on the CH path imports Docker.

Each entry is a list of architecture aliases; the FIRST element is the
canonical form (e.g. "linux/amd64").
"""
from src.utils.config import ConfigManager

config = ConfigManager()

# Architectures this node can BUILD/pack for. Retained for completeness; packing
# itself is now delegated to the external packer-service.
PACKER_SUPPORTED_ARCHITECTURES = []
if config.get("packer.ARM_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(["linux/arm64", "arm64", "arm_64", "aarch64"])
if config.get("packer.X86_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(["linux/amd64", "x86_64", "amd64"])

# Architectures this node can RUN (Cloud Hypervisor).
SUPPORTED_ARCHITECTURES = []
if config.get("builder.ARM_SUPPORT"):
    SUPPORTED_ARCHITECTURES.append(["linux/arm64", "arm64", "arm_64", "aarch64"])
if config.get("builder.X86_SUPPORT"):
    SUPPORTED_ARCHITECTURES.append(["linux/amd64", "x86_64", "amd64"])
