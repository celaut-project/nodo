"""Helpers for generated protobuf modules."""

from pathlib import Path
import sys


# Protobuf Python generation uses absolute imports (e.g. `import buffer_pb2`).
# Keep this directory on sys.path so those imports resolve when loaded as
# `from protos import ...`.
PROTO_DIR = str(Path(__file__).resolve().parent)
if PROTO_DIR not in sys.path:
    sys.path.append(PROTO_DIR)
