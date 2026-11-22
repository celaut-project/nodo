"""The canonical encoding of the messages that have an id (docs/CANONICAL_ENCODING.md).

The id of a service is the hash of its bytes, so the same service must have one set of
bytes. Protobuf does not give that: a reader accepts fields in any order, a field that is
written twice, a varint with extra bytes and a field at its default value, and each gives
the same message with other bytes. This module accepts a byte string if and only if it is
the encoding that a writer of this subset produces. It does not parse into a message and
it needs no network, no configuration and no database, so a reader of signed evidence or
another tool can use it.

The rules, for the schema that is given (:func:`check`):

1. A key is ``number * 8 + class``. Only the class 0 (a varint follows) and the class 2
   (a length and that many bytes follow) are used. Any other class is refused.
2. The fields are in ascending order of number. A field that is not repeated is written at
   most once. A repeated string, bytes or message field has its elements one after the
   other.
3. A varint has no extra bytes: the last byte is not zero unless the value is zero.
4. A number is unsigned and fits its type (``uint64``, ``uint32``, ``int32`` that is not
   negative, ``bool`` as 0 or 1, ``enum`` that is not negative). A negative value is
   refused: it needs 10 bytes and there is no use for it.
5. A field with no presence (a proto3 scalar, string or bytes that is not ``optional``) is
   not written at its default value. A field with presence (a message, an ``optional``
   field, a member of a ``oneof``) is written if and only if it is set, also when it is
   empty.
6. A repeated number is packed (one length-delimited field) and not empty.
7. A string is valid UTF-8.
8. At most one member of a ``oneof`` is present.
9. No ``map``, group, ``float``, ``double``, signed, 64-bit signed or fixed-width type.

A field that the schema does not know (a newer writer) is skipped, so a node with an older
schema still checks everything it can: the order of the keys, the classes and the varints
(``unknown="skip"``). It cannot check the rules that need the type, and it never writes the
message again: a reader that has an id forwards the bytes as they came. ``unknown="refuse"``
refuses the field.
"""
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from google.protobuf.descriptor import Descriptor, FieldDescriptor as F

#: The classes of a key that the encoding uses. The numbers are those of the protobuf wire
#: format, which is what makes these bytes readable by any protobuf library.
VARINT = 0
LENGTH = 2

#: The deepest nesting of messages that a reader follows.
MAX_DEPTH = 200

#: ``type -> largest value`` of the number types that the encoding allows.
_NUMBER_MAX = {
    F.TYPE_UINT64: 2 ** 64 - 1,
    F.TYPE_UINT32: 2 ** 32 - 1,
    F.TYPE_INT32: 2 ** 31 - 1,
    F.TYPE_BOOL: 1,
    F.TYPE_ENUM: 2 ** 31 - 1,
}
_LENGTH_TYPES = frozenset((F.TYPE_STRING, F.TYPE_BYTES, F.TYPE_MESSAGE))
ALLOWED_TYPES = frozenset(_NUMBER_MAX) | _LENGTH_TYPES


class NotCanonical(ValueError):
    """The bytes are not the canonical encoding. ``path`` says where, ``reason`` why."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _is_map(field: F) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def _is_real_oneof(field: F) -> bool:
    """A member of a ``oneof`` that the schema writes, not the one of an ``optional``."""
    oneof = field.containing_oneof
    return oneof is not None and not (
        len(oneof.fields) == 1 and field.has_presence and oneof.name.startswith("_"))


def schema_violations(descriptor: Descriptor, _seen: Optional[set] = None) -> List[str]:
    """The fields of ``descriptor`` and of the messages that it holds that the encoding does
    not allow. Empty for a schema that this module can check. A test calls it on the schema
    of a service, so a type that is not allowed cannot enter it by accident."""
    seen = set() if _seen is None else _seen
    if descriptor.full_name in seen:
        return []
    seen.add(descriptor.full_name)
    found: List[str] = []
    for field in descriptor.fields:
        where = f"{descriptor.full_name}.{field.name}"
        if _is_map(field):
            found.append(f"{where}: a map")
        elif field.type not in ALLOWED_TYPES:
            found.append(f"{where}: a field of the type {field.type}")
        elif field.message_type is not None:
            found.extend(schema_violations(field.message_type, seen))
    return found


@lru_cache(maxsize=None)
def _fields_by_number(descriptor: Descriptor) -> Dict[int, F]:
    return {field.number: field for field in descriptor.fields}


def _varint(data, pos: int, end: int, path: str) -> Tuple[int, int]:
    value = shift = 0
    start = pos
    while True:
        if pos >= end:
            raise NotCanonical(path, f"a varint is cut at {start}")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        if shift >= 70:
            raise NotCanonical(path, f"a varint of more than 10 bytes at {start}")
    if pos - start > 1 and data[pos - 1] == 0:
        raise NotCanonical(path, f"a varint with extra bytes at {start}")
    if value > 2 ** 64 - 1:
        raise NotCanonical(path, f"a varint that does not fit 64 bits at {start}")
    return value, pos


def check(descriptor: Descriptor, data, *, unknown: str = "skip") -> None:
    """Raise :class:`NotCanonical` unless ``data`` is the canonical encoding of a message of
    ``descriptor``. ``data`` is bytes, a ``memoryview`` or a map of a file."""
    if unknown not in ("skip", "refuse"):
        raise ValueError("unknown is 'skip' or 'refuse'")
    _message(descriptor, data, 0, len(data), descriptor.name, unknown == "skip", 0)


def is_canonical(descriptor: Descriptor, data, *, unknown: str = "skip") -> bool:
    try:
        check(descriptor, data, unknown=unknown)
    except NotCanonical:
        return False
    return True


def _message(descriptor: Descriptor, data, pos: int, end: int, path: str,
             skip_unknown: bool, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise NotCanonical(path, f"nested deeper than {MAX_DEPTH} messages")
    fields = _fields_by_number(descriptor)
    last = 0
    oneofs: Dict[str, int] = {}
    while pos < end:
        key, pos = _varint(data, pos, end, path)
        number, klass = key >> 3, key & 7
        if number == 0:
            raise NotCanonical(path, "the field number 0")
        field = fields.get(number)
        where = path if field is None else f"{path}.{field.name}"
        if klass not in (VARINT, LENGTH):
            raise NotCanonical(where, f"the class {klass} of a key is not used")
        if number < last:
            raise NotCanonical(where, f"the field {number} comes after the field {last}")

        if field is None:
            if not skip_unknown:
                raise NotCanonical(where, f"the field {number} is not in the schema")
            last = number
            if klass == VARINT:
                _, pos = _varint(data, pos, end, where)
            else:
                length, pos = _varint(data, pos, end, where)
                if pos + length > end:
                    raise NotCanonical(where, "a length goes past the message")
                pos += length
            continue

        repeated = field.label == F.LABEL_REPEATED
        if number == last and not (repeated and field.type in _LENGTH_TYPES):
            raise NotCanonical(where, f"the field {number} is written twice")
        last = number
        if _is_map(field):
            raise NotCanonical(where, "a map is not allowed")
        if field.type not in ALLOWED_TYPES:
            raise NotCanonical(where, f"a field of the type {field.type} is not allowed")
        if _is_real_oneof(field):
            name = field.containing_oneof.name
            if oneofs.setdefault(name, number) != number:
                raise NotCanonical(where, f"two members of the oneof {name} are present")

        if field.type in _NUMBER_MAX:
            if repeated:
                if klass != LENGTH:
                    raise NotCanonical(where, "a list of numbers is not packed")
                length, pos = _varint(data, pos, end, where)
                stop = pos + length
                if stop > end:
                    raise NotCanonical(where, "a length goes past the message")
                if length == 0:
                    raise NotCanonical(where, "a packed list that is empty")
                while pos < stop:
                    value, pos = _varint(data, pos, stop, where)
                    if value > _NUMBER_MAX[field.type]:
                        raise NotCanonical(where, f"{value} does not fit the type")
                continue
            if klass != VARINT:
                raise NotCanonical(where, "a number with the class of a length")
            value, pos = _varint(data, pos, end, where)
            if value > _NUMBER_MAX[field.type]:
                raise NotCanonical(where, f"{value} does not fit the type (negative?)")
            if value == 0 and not field.has_presence:
                raise NotCanonical(where, "a value at its default is written")
            continue

        if klass != LENGTH:
            raise NotCanonical(where, "a length-delimited field with the class of a number")
        length, pos = _varint(data, pos, end, where)
        stop = pos + length
        if stop > end:
            raise NotCanonical(where, "a length goes past the message")
        if field.type == F.TYPE_MESSAGE:
            _message(field.message_type, data, pos, stop, where, skip_unknown, depth + 1)
        else:
            if length == 0 and not field.has_presence and not repeated:
                raise NotCanonical(where, "a value at its default is written")
            if field.type == F.TYPE_STRING:
                try:
                    bytes(data[pos:stop]).decode("utf-8")
                except UnicodeDecodeError:
                    raise NotCanonical(where, "a string that is not UTF-8") from None
        pos = stop
