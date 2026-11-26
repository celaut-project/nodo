"""Read a descriptor file of ``protos/descriptors/`` (see ``protos/descriptors/REASON.md``).

A descriptor file holds one ``celaut.Descriptor``: a ``tags:`` line, the prose, and the
formal after the line ``=== formal``. The formal is the bytes after that line, up to the
end of the file, without the last newline. Only these bytes are the identity of the
descriptor: a change to the tags or to the prose does not change its id.

Each folder holds the descriptors of one use: one place, or a group of places, of
``celaut.proto``. The ``REASON.md`` of a folder gives its class in a line ``Class: `x```.

Run this module to write the generated part of ``protos/descriptors/INDEX.md``:
``python -m src.utils.descriptor_file``.
"""
import hashlib
import os
import re
from typing import List, NamedTuple, Optional, Tuple

SUFFIX = ".descriptor"
TAGS_PREFIX = b"tags:"
MARKER = b"\n=== formal\n"
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "protos", "descriptors")
INDEX = "INDEX.md"
FOLDER_RULES = "REASON.md"
CLASS_LINE = re.compile(r"^Class: `([a-z-]+)`\.?$", re.MULTILINE)
BEGIN = "<!-- BEGIN GENERATED: python -m src.utils.descriptor_file -->"
END = "<!-- END GENERATED -->"


class DescriptorFileError(ValueError):
    """The bytes of a file do not follow the format of a descriptor file."""


class DescriptorFile(NamedTuple):
    tags: List[str]
    prose: str
    formal: bytes


def parse(data: bytes) -> DescriptorFile:
    """The tags, the prose and the formal of the bytes of a descriptor file."""
    if b"\r" in data:
        raise DescriptorFileError("the file has a carriage return: use LF only")
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise DescriptorFileError("the file must end with exactly one newline")
    if not data.startswith(TAGS_PREFIX):
        raise DescriptorFileError("the first line must start with 'tags:'")
    if data.count(MARKER) != 1:
        raise DescriptorFileError("the file must have exactly one line '=== formal'")
    head, formal = data.split(MARKER)
    formal = formal[:-1]
    if not formal:
        raise DescriptorFileError("the formal is empty")
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DescriptorFileError(f"the tags and the prose are not UTF-8: {e}") from None
    first, _, prose = text.partition("\n")
    tags = [t.strip() for t in first[len("tags:"):].split(",") if t.strip()]
    return DescriptorFile(tags=tags, prose=prose.strip("\n"), formal=formal)


def read(path: str) -> DescriptorFile:
    """The descriptor in the file at ``path``."""
    with open(path, "rb") as f:
        return parse(f.read())


def hash_type() -> str:
    """The type of the hash of an id: the digest of the empty input (REASON 6.3)."""
    return hashlib.blake2b(b"", digest_size=32).hexdigest()


def formal_id(formal: bytes) -> str:
    """The id of a formal, ``<type>:<digest>``, with the algorithm that nodo uses."""
    return f"{hash_type()}:{hashlib.blake2b(formal, digest_size=32).hexdigest()}"


def files(root: str = ROOT) -> List[str]:
    """The paths of all the descriptor files under ``root``, relative and sorted."""
    out = []
    for folder, _, names in os.walk(root):
        for name in names:
            if name.endswith(SUFFIX):
                out.append(os.path.relpath(os.path.join(folder, name), root))
    return sorted(out)


def folders(root: str = ROOT) -> List[str]:
    """The folders under ``root`` that have their own rules file, relative and sorted."""
    out = []
    for folder, _, names in os.walk(root):
        if FOLDER_RULES in names and os.path.abspath(folder) != os.path.abspath(root):
            out.append(os.path.relpath(folder, root))
    return sorted(out)


def folder_class(folder: str, root: str = ROOT) -> Optional[str]:
    """The class that the rules file of ``folder`` gives, or None for a group folder."""
    with open(os.path.join(root, folder, FOLDER_RULES), encoding="utf-8") as f:
        m = CLASS_LINE.search(f.read())
    return m.group(1) if m else None


def index_rows(root: str = ROOT) -> List[Tuple[str, str, str]]:
    """One row for each file: the path, the tags and the id of the formal."""
    rows = []
    for rel in files(root):
        d = read(os.path.join(root, rel))
        rows.append((rel, ", ".join(d.tags), formal_id(d.formal)))
    return rows


def render_generated(root: str = ROOT) -> str:
    """The generated part of ``INDEX.md``, between its two marker lines."""
    lines = [
        BEGIN,
        "",
        "The id is `<type>:<digest>` of the formal (REASON 6.3), with blake2b-256: the",
        "algorithm that nodo uses. Another algorithm gives other ids for the same bytes.",
        "",
        "| File | Tags | Id |",
        "| --- | --- | --- |",
    ]
    for rel, tags, fid in index_rows(root):
        lines.append(f"| [`{rel}`]({rel}) | {tags} | `{fid}` |")
    lines += ["", END]
    return "\n".join(lines)


def generated_part(text: str) -> str:
    """The part of ``text`` between the two marker lines, with the markers."""
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        raise ValueError(f"{INDEX} has no generated part")
    return text[start:end + len(END)]


def main() -> None:
    path = os.path.join(ROOT, INDEX)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = text.replace(generated_part(text), render_generated())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    main()
