"""Reading and building the ``repeated *KeyValue`` fields that replaced protobuf maps.

``xattrs``, ``mu_per_call``, ``environment_variables`` and ``benchmark`` used to be
``map<string, V>``. A map gives lookup by key and nothing about order on the wire, and a
service id is a hash of the serialized specification, so the same service could hash to
two ids depending on the protobuf implementation. They are now
``repeated BytesKeyValue`` / ``AmountKeyValue`` / ``DataFormatKeyValue`` /
``Uint64KeyValue`` at the same field numbers -- wire-identical to the map they replace,
with an order that is kept.

This module is the one place that says how such a list is read and written; nothing else
should loop over the entries by hand.

* **Writing** (``from_dict``, ``set_value``, ``update``, ``delete``): the list is left
  sorted by key -- ascending by UTF-8 bytes, which is Python's ``str`` order too -- with
  exactly one entry per key and no empty key. Every producer that follows this writes the
  same bytes for the same content, whatever order it learned the content in.
* **Reading** (``to_dict``, ``get``, ``contains``): a key resolves to the LAST entry that
  carries it. That is what parsing a ``map`` did when a key repeated, so a service that a
  map-based node wrote and one an entry-list node wrote read the same. A received list is
  not required to be sorted or duplicate-free, and is never reordered just by reading it.
* **Refusing** (``check_unique``): where a node builds a list from something an author
  wrote, a repeated key is an error rather than a silent last-wins, because the author
  meant one of the two values and the packer cannot know which.

The value is written through the entry's own ``value`` field: assigned for the scalar
kinds (``bytes``, ``uint64``; declared ``optional`` in the proto so an empty value or a
0 is still written, as a map entry always did), copied for the message kinds.
"""
import json
from typing import Any, Iterable, List, Mapping, MutableMapping, Tuple

from google.protobuf import json_format
from google.protobuf.message import Message


class DuplicateKeyError(ValueError):
    """A key list that should name each key once names one twice."""


def _assign(entry: Any, value: Any) -> None:
    if isinstance(value, Message):
        entry.value.CopyFrom(value)
    else:
        entry.value = value


def _value(entry: Any) -> Any:
    return entry.value


def to_dict(entries: Iterable[Any]) -> MutableMapping[str, Any]:
    """``{key: value}`` in entry order, the last entry winning a repeated key.

    A fresh ``dict``: changing it changes nothing in ``entries``. Message values are the
    entries' own sub-messages, so mutate those through :func:`set_value` instead.
    """
    return {entry.key: _value(entry) for entry in entries}


def get(entries: Iterable[Any], key: str, default: Any = None) -> Any:
    """The value of ``key``'s last entry, or ``default`` when there is none."""
    found = default
    for entry in entries:
        if entry.key == key:
            found = _value(entry)
    return found


def contains(entries: Iterable[Any], key: str) -> bool:
    return any(entry.key == key for entry in entries)


def duplicate_keys(entries: Iterable[Any]) -> List[str]:
    """Every key that appears more than once, sorted."""
    seen = set()
    repeated = set()
    for entry in entries:
        (repeated if entry.key in seen else seen).add(entry.key)
    return sorted(repeated)


def check_unique(entries: Iterable[Any], what: str = "key/value list") -> None:
    """Raise :class:`DuplicateKeyError` naming the repeated keys, or return."""
    repeated = duplicate_keys(entries)
    if repeated:
        raise DuplicateKeyError(f"{what} repeats key(s): {', '.join(repeated)}.")


def _check_key(key: Any) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError(f"key/value keys must be non-empty strings, got {key!r}.")
    return key


def _sorted_items(mapping: Mapping[str, Any]) -> List[Tuple[str, Any]]:
    return [(_check_key(key), mapping[key]) for key in sorted(mapping, key=str)]


def from_dict(entries: Any, mapping: Mapping[str, Any]) -> None:
    """Replace the contents of the repeated field ``entries`` with ``mapping``, sorted.

    ``entries`` is the field itself (``message.xattrs``). It is filled in place, which is
    the only way to fill a protobuf repeated field.
    """
    items = _sorted_items(mapping)
    del entries[:]
    for key, value in items:
        entry = entries.add()
        entry.key = key
        _assign(entry, value)


def update(entries: Any, mapping: Mapping[str, Any]) -> None:
    """Set every key of ``mapping``, keeping the rest, and leave the list canonical.

    Like ``dict.update`` on the map this replaces: an existing key takes the new value.
    The whole list is rewritten sorted, with any repeated key already in it collapsed to
    the value it would have read as.
    """
    merged = to_dict(entries)
    # Detach message values before the list is cleared under them.
    merged = {key: _detached(value) for key, value in merged.items()}
    for key in mapping:
        _check_key(key)
    merged.update(mapping)
    from_dict(entries, merged)


def set_value(entries: Any, key: str, value: Any) -> None:
    """Set one key, leaving the list canonical. See :func:`update`."""
    update(entries, {key: value})


def delete(entries: Any, key: str) -> bool:
    """Remove every entry for ``key``; True if there was one."""
    kept = {k: _detached(v) for k, v in to_dict(entries).items() if k != key}
    if len(kept) == len(set(entry.key for entry in entries)):
        return False
    from_dict(entries, kept)
    return True


def _detached(value: Any) -> Any:
    if isinstance(value, Message):
        copy = type(value)()
        copy.CopyFrom(value)
        return copy
    return value


def is_canonical(entries: Iterable[Any]) -> bool:
    """Whether the list is what a producer following this module would have written."""
    keys = [entry.key for entry in entries]
    return all(keys) and keys == sorted(set(keys))


def keys(entries: Iterable[Any]) -> Tuple[str, ...]:
    """The distinct keys, sorted -- independent of the order they arrived in."""
    return tuple(sorted({entry.key for entry in entries}))


def items(entries: Iterable[Any]) -> List[Tuple[str, Any]]:
    """``(key, value)`` pairs sorted by key, last entry winning a repeated key."""
    resolved = to_dict(entries)
    return [(key, resolved[key]) for key in sorted(resolved)]



# ---------------------------------------------------------------------------
# service.json: keep the object syntax
# ---------------------------------------------------------------------------
#
# ``service.json`` is written by people, and a JSON object -- ``{"KEY": value}`` -- is
# how they have always written these fields (and how protobuf's own JSON mapping wrote
# a ``map``). The wire format changed; the file format did not. An object is converted
# to the entry list when the file is read, and the list is what gets sorted and packed,
# so the order an author happened to type keys in never reaches a service id.

_ENTRY_MESSAGES = frozenset({
    "celaut.BytesKeyValue",
    "celaut.AmountKeyValue",
    "celaut.DataFormatKeyValue",
    "celaut.Uint64KeyValue",
})


class JsonObject(dict):
    """A ``dict`` read from JSON that remembers the keys the file repeated.

    ``json`` keeps the last of a repeated key and forgets there were two, which for a
    price or an environment variable means silently choosing one of two values the
    author wrote. Load with :func:`json_object_hook` and :func:`check_json_object`
    can say so.
    """

    duplicates: Tuple[str, ...] = ()


def json_object_hook(pairs: List[Tuple[str, Any]]) -> JsonObject:
    """``object_pairs_hook`` for ``json.load`` that records repeated keys."""
    obj = JsonObject()
    repeated = set()
    for key, value in pairs:
        if key in obj:
            repeated.add(key)
        obj[key] = value
    obj.duplicates = tuple(sorted(repeated))
    return obj


def check_json_object(obj: Any, path: str) -> None:
    """Raise :class:`DuplicateKeyError` if the JSON object at ``path`` repeated a key."""
    repeated = getattr(obj, "duplicates", ())
    if repeated:
        raise DuplicateKeyError(
            f"service.json {path} repeats key(s): {', '.join(repeated)}. "
            "Each key may be written once."
        )


def _entries_from_json(value: Any, path: str) -> Any:
    """A key/value field's JSON as a sorted ``[{"key": .., "value": ..}]`` list.

    An object (the ``service.json`` syntax) and a list of entries (protobuf's own
    JSON shape for the message) are both accepted. Anything else is returned as it is
    for the protobuf parser to refuse with its own message.
    """
    if isinstance(value, Mapping):
        check_json_object(value, path)
        return [{"key": key, "value": value[key]} for key in sorted(value, key=str)]
    if isinstance(value, list) and all(
        isinstance(item, Mapping) and isinstance(item.get("key"), str) for item in value
    ):
        repeated = sorted({
            item["key"] for i, item in enumerate(value)
            if any(other["key"] == item["key"] for other in value[:i])
        })
        if repeated:
            raise DuplicateKeyError(
                f"service.json {path} repeats key(s): {', '.join(repeated)}."
            )
        return sorted(value, key=lambda item: item["key"])
    return value


def json_objects_to_entries(document: Any, descriptor: Any, path: str = "") -> Any:
    """``document`` (protobuf JSON for ``descriptor``) with every key/value object
    rewritten as a canonical entry list, ready for ``json_format.ParseDict``.

    Walks the message by its descriptor, so it finds these fields at any depth (an
    embedded workload dependency's ``container.environment_variables``, a slot's
    ``mu_per_call``) without a list of paths to keep in step with the proto. Returns a
    new structure; ``document`` is not modified.
    """
    if not isinstance(document, Mapping):
        return document

    fields = {}
    for field in descriptor.fields:
        fields[field.name] = field
        fields[field.json_name] = field

    converted = {}
    for key, value in document.items():
        field = fields.get(key)
        if field is None or field.message_type is None:
            converted[key] = value
            continue
        here = f"{path}.{key}" if path else str(key)
        if field.message_type.full_name in _ENTRY_MESSAGES:
            converted[key] = _entries_from_json(value, here)
        elif field.label == field.LABEL_REPEATED and isinstance(value, list):
            converted[key] = [
                json_objects_to_entries(item, field.message_type, f"{here}[{i}]")
                for i, item in enumerate(value)
            ]
        else:
            converted[key] = json_objects_to_entries(value, field.message_type, here)
    return converted


# ---------------------------------------------------------------------------
# JSON out: the shape that was published before the fields became lists
# ---------------------------------------------------------------------------
#
# A Peer is published as JSON (the R9 of an on-chain reputation proof, and the record
# submitted for a peer). Whoever reads that JSON was written against the object a map
# produced -- ``"muPerCall": {"exec": {"n": "10"}}`` -- and parsing it back with
# protobuf, or with a reader for the previous release, expects it. So the entry lists are
# written back out as objects. ``json_objects_to_entries`` reads that shape in again.
#
# An object cannot say "twice" or "in this order": a repeated key collapses to the last
# (the reading rule above) and keys come out sorted. The JSON is a publication of what a
# reader would resolve, not a second serialization of the message; what is signed is
# computed from the entries, never from this text.

def _entries_to_objects(document: Any, descriptor: Any) -> Any:
    if not isinstance(document, dict):
        return document

    fields = {field.json_name: field for field in descriptor.fields}
    converted = {}
    for key, value in document.items():
        field = fields.get(key)
        if field is None or field.message_type is None:
            converted[key] = value
        elif field.message_type.full_name in _ENTRY_MESSAGES and isinstance(value, list):
            value_field = field.message_type.fields_by_name["value"]
            entries = {}
            for item in value:
                if "value" in item:
                    entry_value = item["value"]
                elif value_field.message_type is not None:
                    entry_value = {}
                elif value_field.type == value_field.TYPE_BYTES:
                    entry_value = ""
                else:
                    entry_value = "0"
                if value_field.message_type is not None:
                    entry_value = _entries_to_objects(entry_value, value_field.message_type)
                entries[item.get("key", "")] = entry_value
            converted[key] = entries
        elif field.label == field.LABEL_REPEATED and isinstance(value, list):
            converted[key] = [_entries_to_objects(item, field.message_type) for item in value]
        else:
            converted[key] = _entries_to_objects(value, field.message_type)
    return converted


def message_to_dict(message: Message) -> dict:
    """``json_format.MessageToDict`` with the key/value fields as objects."""
    return _entries_to_objects(json_format.MessageToDict(message), message.DESCRIPTOR)


def message_to_json(message: Message) -> str:
    """``json_format.MessageToJson`` with the key/value fields as objects."""
    return json.dumps(message_to_dict(message), indent=2)
