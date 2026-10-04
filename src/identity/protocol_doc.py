"""The prose of the celaut-gateway layer, read from the comments of ``celaut.proto``.

``formal`` identifies a message field only by its number, because a name never travels
on the wire (``protocol_schema``). A reader who must decide if two declarations are the
same protocol after a conflict -- a person or a language model -- also needs what each
RPC, message and field means. The leading comment of each element in ``celaut.proto``
gives this, in ASD-STE100 Simplified Technical English, and this module puts those
comments into the prose next to the names and the numbers.

The comments are the only copy of that text. The generated ``celaut_pb2`` does not keep
comments, and the node has no ``protoc`` at run time, so ``bash/generate_protos.sh``
also writes ``protos/celaut_doc.binpb``: the same descriptor with its source info
(``--include_source_info``). This module reads that file.

Which comments are read:

- The leading comment of a message, a field or an RPC: the comment immediately above
  it, with no blank line between. That is the protocol description.
- Not a detached comment (a blank line between it and the element), and not a trailing
  comment. These are notes for the developers of this implementation.
"""
import functools
import os
import re
from typing import Dict, List, Optional, Tuple

from google.protobuf import descriptor_pb2

from protos import celaut_pb2

DOC_DESCRIPTOR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(celaut_pb2.__file__)), "celaut_doc.binpb"
)

# Field numbers in descriptor.proto, which make the paths of SourceCodeInfo.Location.
_FILE_MESSAGE_TYPE = 4
_FILE_SERVICE = 6
_MESSAGE_FIELD = 2
_MESSAGE_NESTED_TYPE = 3
_SERVICE_METHOD = 2

_LABELS = {
    descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED: "repeated",
}


def _clean(comment: str) -> str:
    """A leading comment as one line of text: the ``//`` margins and line breaks removed."""
    return re.sub(r"\s+", " ", comment).strip()


def load_file_descriptor(path: str = DOC_DESCRIPTOR_PATH) -> descriptor_pb2.FileDescriptorProto:
    """``celaut.proto`` as ``protos/celaut_doc.binpb`` gives it, with its comments."""
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    with open(path, "rb") as f:
        descriptor_set.ParseFromString(f.read())
    for file in descriptor_set.file:
        if file.name == celaut_pb2.DESCRIPTOR.name:
            return file
    raise ValueError(f"{path} does not contain {celaut_pb2.DESCRIPTOR.name}.")


def _comments(file: descriptor_pb2.FileDescriptorProto) -> Dict[Tuple[int, ...], str]:
    return {
        tuple(location.path): _clean(location.leading_comments)
        for location in file.source_code_info.location
        if location.leading_comments.strip()
    }


@functools.lru_cache(maxsize=1)
def descriptions() -> Dict[str, str]:
    """``{full name: description}`` for every message, field and Gateway RPC.

    A message is ``celaut.Peer``, a field ``celaut.Peer.ts``, an RPC
    ``celaut.Gateway.StartService``. An element with no leading comment is not in it.
    """
    file = load_file_descriptor()
    comments = _comments(file)
    found: Dict[str, str] = {}

    def walk(messages, prefix: str, path: Tuple[int, ...], kind: int) -> None:
        for i, message in enumerate(messages):
            name = f"{prefix}.{message.name}"
            message_path = path + (kind, i)
            if message_path in comments:
                found[name] = comments[message_path]
            for j, field in enumerate(message.field):
                field_path = message_path + (_MESSAGE_FIELD, j)
                if field_path in comments:
                    found[f"{name}.{field.name}"] = comments[field_path]
            walk(message.nested_type, name, message_path, _MESSAGE_NESTED_TYPE)

    walk(file.message_type, file.package, (), _FILE_MESSAGE_TYPE)
    for s, service in enumerate(file.service):
        for m, method in enumerate(service.method):
            path = (_FILE_SERVICE, s, _SERVICE_METHOD, m)
            if path in comments:
                found[f"{file.package}.{service.name}.{method.name}"] = comments[path]
    return found


def undescribed(file_descriptor=celaut_pb2.DESCRIPTOR) -> List[str]:
    """Every message, field and Gateway RPC that has no description, by full name."""
    from src.identity.protocol_schema import _messages

    known = descriptions()
    missing: List[str] = []
    for message in _messages(file_descriptor.message_types_by_name.values()):
        names = [message.full_name] + [f.full_name for f in message.fields]
        missing.extend(n for n in names if n not in known)
    for service in file_descriptor.services_by_name.values():
        missing.extend(m.full_name for m in service.methods if m.full_name not in known)
    return missing


def _field_line(field) -> str:
    from src.identity.protocol_schema import _field

    number, cardinality, type_name = _field(field).split(":", 2)
    text = descriptions().get(field.full_name, "")
    return f"  Field {number}, {field.name}: {cardinality} {type_name}. {text}".rstrip()


def _oneof_lines(message) -> List[str]:
    from src.identity.protocol_schema import _is_synthetic_oneof

    lines = []
    for oneof in message.oneofs:
        if len(oneof.fields) == 1 and _is_synthetic_oneof(oneof.fields[0]):
            continue
        numbers = sorted(f.number for f in oneof.fields)
        lines.append(
            f"  Fields {', '.join(str(n) for n in numbers)} are one oneof: a message "
            "sets one of them at most. If a sender sets more, the last on the wire is "
            "correct."
        )
    return lines


def messages_prose(file_descriptor=celaut_pb2.DESCRIPTOR) -> str:
    """One entry for each message: its full name and description, then its fields."""
    from src.identity.protocol_schema import _messages

    entries = []
    for message in _messages(file_descriptor.message_types_by_name.values()):
        lines = [f"{message.full_name}. {descriptions().get(message.full_name, '')}".rstrip()]
        lines.extend(_field_line(f) for f in sorted(message.fields, key=lambda f: f.number))
        lines.extend(_oneof_lines(message))
        entries.append("\n".join(lines))
    return "\n\n".join(entries)


def method_prose(service_name: str, method: str) -> Optional[str]:
    """The description of one RPC of ``service_name``, or None if it has none."""
    return descriptions().get(f"{celaut_pb2.DESCRIPTOR.package}.{service_name}.{method}")
