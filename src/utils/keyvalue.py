"""Reading and building the ``repeated *KeyValue`` fields that replaced protobuf maps.

``xattrs``, ``mu_per_call``, ``environment_variables`` and ``min_benchmark`` used to be
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
from typing import Any, Iterable, List, Mapping, MutableMapping, Tuple

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

