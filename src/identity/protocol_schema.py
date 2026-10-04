"""The message schema a protocol layer carries, as canonical ``formal`` lines.

A layer whose payload is protobuf is only described in full if its message schema is
part of the description: two nodes that both say ``bee-rpc`` or ``celaut-gateway``
and disagree on a field number cannot talk, and nothing else in the announcement
would show it. So the schema rides in ``formal`` itself, whole, one ``key=value`` line
per message -- not a hash pointing at a document the reader would have to find
somewhere else.

Only what the binary encoding depends on is written: each field's number, its
cardinality and its type. Field names are left out, because they never travel in the
encoding, so two nodes that name a field differently still read each other's bytes
-- by the criterion every layer follows, something both sides do not have to agree on
is not part of the protocol. Message names are kept, as the only way one line can
refer to another.

One line per message, keyed ``schema.<full message name>``, each field written as
``<number>:<cardinality>:<type>`` and the fields joined by ``,`` in field-number order:

``<cardinality>``
    ``singular`` (proto3 implicit presence), ``optional`` (explicit presence),
    ``repeated``, ``map``, or ``oneof.<number>`` for a member of a real oneof -- the
    members of one oneof replace each other on the wire, which a reader has to know.
    ``<number>`` is the lowest field number of the oneof's members. Each member
    already has its own field number on the wire, but the oneof's name does not
    travel, so the name is not part of the protocol.
``<type>``
    The protobuf scalar name (``int32``, ``bytes``, ...), the full name of a message,
    or ``map<key,value>`` for a map.

A message with no fields is written with an empty value (``schema.buffer.Empty=``).
Map entry messages are not written on their own: the ``map<...>`` on the field that
uses one says all there is to say.
"""
from typing import Dict, Iterable

from google.protobuf.descriptor import Descriptor, FieldDescriptor

_SCALAR_NAMES = {
    FieldDescriptor.TYPE_DOUBLE: "double",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int64",
    FieldDescriptor.TYPE_UINT64: "uint64",
    FieldDescriptor.TYPE_INT32: "int32",
    FieldDescriptor.TYPE_FIXED64: "fixed64",
    FieldDescriptor.TYPE_FIXED32: "fixed32",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "uint32",
    FieldDescriptor.TYPE_SFIXED32: "sfixed32",
    FieldDescriptor.TYPE_SFIXED64: "sfixed64",
    FieldDescriptor.TYPE_SINT32: "sint32",
    FieldDescriptor.TYPE_SINT64: "sint64",
}


def _is_map_entry(message: Descriptor) -> bool:
    return bool(message.GetOptions().map_entry)


def _is_repeated(field: FieldDescriptor) -> bool:
    # `label` was removed in protobuf 7 in favour of `is_repeated`; the same fallback
    # bee_rpc.utils.is_repeated_message_field makes.
    is_repeated = getattr(field, "is_repeated", None)
    if is_repeated is None:
        is_repeated = field.label == FieldDescriptor.LABEL_REPEATED
    return bool(is_repeated)


def _type_name(field: FieldDescriptor) -> str:
    if field.type in (FieldDescriptor.TYPE_MESSAGE, FieldDescriptor.TYPE_GROUP):
        return field.message_type.full_name
    if field.type == FieldDescriptor.TYPE_ENUM:
        return field.enum_type.full_name
    return _SCALAR_NAMES[field.type]


def _is_synthetic_oneof(field: FieldDescriptor) -> bool:
    """Whether ``field``'s oneof is the one protoc makes for a proto3 ``optional``."""
    oneof = field.containing_oneof
    return oneof is not None and len(oneof.fields) == 1 and oneof.name == f"_{field.name}"


def _field(field: FieldDescriptor) -> str:
    if field.type == FieldDescriptor.TYPE_MESSAGE and _is_map_entry(field.message_type):
        entry = field.message_type.fields_by_name
        return f"{field.number}:map:map<{_type_name(entry['key'])},{_type_name(entry['value'])}>"
    if _is_repeated(field):
        cardinality = "repeated"
    elif field.containing_oneof is not None and not _is_synthetic_oneof(field):
        cardinality = f"oneof.{min(f.number for f in field.containing_oneof.fields)}"
    elif _is_synthetic_oneof(field) or field.type == FieldDescriptor.TYPE_MESSAGE:
        # A singular message field always tracks presence, in proto3 too.
        cardinality = "optional"
    else:
        cardinality = "singular"
    return f"{field.number}:{cardinality}:{_type_name(field)}"


def _messages(messages: Iterable[Descriptor]):
    for message in messages:
        if _is_map_entry(message):
            continue
        yield message
        yield from _messages(message.nested_types)


def schema_pairs(file_descriptor) -> Dict[str, str]:
    """``schema.<message>`` -> its fields, for every message ``file_descriptor`` defines."""
    return {
        f"schema.{message.full_name}": ",".join(
            _field(f) for f in sorted(message.fields, key=lambda f: f.number)
        )
        for message in _messages(file_descriptor.message_types_by_name.values())
    }
