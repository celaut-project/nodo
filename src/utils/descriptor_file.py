"""Read a descriptor file of ``contract/descriptors/`` (see ``contract/descriptors/REASON.md``).

A descriptor file holds one ``celaut.Descriptor``: a ``tags:`` line, the prose, and the
formal after the first line ``=== formal``. The formal is the bytes after that line, up to
the end of the file, without the last newline. Only these bytes are the identity of the
descriptor: a change to the tags or to the prose does not change its id. The file does not
hold ``Descriptor.uses``: ``uses`` gives the files that a writer puts in it.

The formal of a document (folder ``documents/``) is the exact bytes of the document. It
is not ``key=value`` lines, and it can hold any byte.

Each folder holds the descriptors of one use: one place, or a group of places, of
``celaut.proto``. The ``REASON.md`` of a folder gives its class in a line ``Class: `x```.

Run this module to write the generated part of ``contract/descriptors/INDEX.md``:
``python -m src.utils.descriptor_file``. With ``--cascade``, it first replaces the old
id of each formal that changed since ``HEAD`` with its new id, in all the files. With
``--stats``, it prints the sizes of the descriptors and of their closures in ``uses``, and
writes nothing.
"""
import hashlib
import os
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

SUFFIX = ".descriptor"
TAGS_PREFIX = b"tags:"
MARKER = b"\n=== formal\n"
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "contract", "descriptors")
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
    if not data.endswith(b"\n"):
        raise DescriptorFileError("the file must end with a newline")
    if not data.startswith(TAGS_PREFIX):
        raise DescriptorFileError("the first line must start with 'tags:'")
    if MARKER not in data:
        raise DescriptorFileError("the file has no line '=== formal'")
    head, formal = data.split(MARKER, 1)
    formal = formal[:-1]
    if b"\r" in head:
        raise DescriptorFileError("the tags or the prose have a carriage return: use LF only")
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


def _blake2b_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The algorithms that this module can check, by their type.
ALGORITHMS = {f(b""): f for f in (_blake2b_256, _sha256)}
ID = re.compile(rb"\b([0-9a-f]{64}):([0-9a-f]{64})\b")
DOCUMENTS = "documents"


def is_document(rel: str) -> bool:
    """True if the file at ``rel`` is a document: its formal is the bytes of the document."""
    return rel.split(os.sep)[0] == DOCUMENTS


def references(formal: bytes) -> List[str]:
    """The ids in a formal of ``key=value`` lines, in the order of their first occurrence.

    ``Descriptor.uses`` holds one descriptor for each of them, in this order (REASON 2.9).
    Do not use it on a document: its bytes are not a formal of ``key=value`` lines.
    """
    out: List[str] = []
    for m in ID.finditer(formal):
        fid = m.group(0).decode()
        if fid not in out:
            out.append(fid)
    return out


_DIGESTS: Dict[Tuple[str, str], Tuple[Tuple[int, int], Dict[str, str]]] = {}


def _digests(root: str, rel: str) -> Dict[str, str]:
    """The digest of the formal of the file ``rel``, for each type of ``ALGORITHMS``.

    A document can be large, so each file is hashed one time, and again only when its
    size or its time of change is different.
    """
    path = os.path.join(root, rel)
    st = os.stat(path)
    key, stamp = (os.path.abspath(root), rel), (st.st_size, st.st_mtime_ns)
    cached = _DIGESTS.get(key)
    if cached is None or cached[0] != stamp:
        formal = read(path).formal
        cached = (stamp, {typ: f(formal) for typ, f in ALGORITHMS.items()})
        _DIGESTS[key] = cached
    return cached[1]


def resolve(fid: str, root: str = ROOT) -> Optional[str]:
    """The file whose formal has the id ``fid``, or None."""
    typ, digest = fid.split(":")
    for rel in files(root):
        if _digests(root, rel).get(typ) == digest:
            return rel
    return None


def uses(formal: bytes, root: str = ROOT) -> List[str]:
    """The files whose formals are the references of ``formal``, in the order of ``uses``.

    It raises ``KeyError`` for an id that no file of ``root`` has, or whose type is not an
    algorithm of ``ALGORITHMS``.
    """
    out = []
    for fid in references(formal):
        rel = resolve(fid, root)
        if rel is None:
            raise KeyError(fid)
        out.append(rel)
    return out


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


def cascade(base: str = "HEAD", root: str = ROOT) -> List[str]:
    """Replace the old id of each changed formal with its new id, in all the files.

    A formal names another by its id (REASON 3.7), so a change of one formal changes the
    id that other formals hold, and so their ids too. This compares each file with its
    version at the git revision ``base`` and repeats until nothing changes. It returns the
    files that it changed.
    """
    import subprocess

    def old_id(rel):
        path = os.path.relpath(os.path.join(root, rel), _repo_root(root))
        done = subprocess.run(["git", "show", f"{base}:{path}"], capture_output=True)
        return formal_id(parse(done.stdout).formal) if done.returncode == 0 else None

    # Each id that a file had: at ``base``, and after each step. A formal can change two
    # times in one run (its own change, then a new id of a formal that it names), and a
    # file can hold the id of any of these versions.
    past = {}
    for rel in files(root):
        past[rel] = {formal_id(read(os.path.join(root, rel)).formal)}
        fid = old_id(rel)
        if fid:
            past[rel].add(fid)
    changed: List[str] = []
    while True:
        mapping = {}
        for rel in files(root):
            new = formal_id(read(os.path.join(root, rel)).formal)
            past.setdefault(rel, set()).add(new)
            for fid in past[rel] - {new}:
                mapping[fid.encode()] = new.encode()
        step = False
        for rel in files(root):
            path = os.path.join(root, rel)
            with open(path, "rb") as f:
                data = f.read()
            new_data = data
            for before, after in mapping.items():
                new_data = new_data.replace(before, after)
            if new_data != data:
                with open(path, "wb") as f:
                    f.write(new_data)
                step = True
                if rel not in changed:
                    changed.append(rel)
        if not step:
            return changed


def _repo_root(path: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


# The set of a typical service (issue #695): its architecture, then the resources and the
# settings that most services hold. ``stats`` gives it one time for each architecture.
TYPICAL_SERVICE = (
    "quantities/resources/memory.descriptor",
    "quantities/resources/cpu.descriptor",
    "quantities/resources/disk.descriptor",
    "policies/filesystem_root/entry_metadata.descriptor",
    "policies/api_slot/access.descriptor",
    "policies/container_init/notice.descriptor",
    "networks/host-name.descriptor",
)
TYPICAL_ARCHITECTURES = ("architectures/linux-amd64.descriptor", "architectures/linux-arm64.descriptor")


class _Closure(NamedTuple):
    tree: int  # the descriptors in uses, at all depths, with each repeat
    tree_bytes: int  # their bytes, with each repeat: the size on the wire
    tree_document_bytes: int
    graph: frozenset  # the descriptors in uses, at all depths, each one time
    missing: frozenset  # the ids that no file holds


def descriptor_bytes(d: DescriptorFile) -> int:
    """The bytes of a descriptor: its tags, its prose and its formal (no protobuf headers)."""
    return sum(len(t.encode("utf-8")) for t in d.tags) + len(d.prose.encode("utf-8")) + len(d.formal)


def _closures(root: str = ROOT):
    sizes, children, missing = {}, {}, {}
    for rel in files(root):
        d = read(os.path.join(root, rel))
        sizes[rel] = descriptor_bytes(d)
        children[rel], missing[rel] = [], set()
        if is_document(rel):
            continue
        for fid in references(d.formal):
            found = resolve(fid, root)
            if found is None:
                missing[rel].add(fid)
            else:
                children[rel].append(found)
    memo: Dict[str, _Closure] = {}

    def closure(rel: str) -> _Closure:
        if rel not in memo:
            tree = tree_bytes = doc_bytes = 0
            graph, lost = set(), set(missing[rel])
            for child in children[rel]:
                sub = closure(child)
                tree += 1 + sub.tree
                tree_bytes += sizes[child] + sub.tree_bytes
                doc_bytes += (sizes[child] if is_document(child) else 0) + sub.tree_document_bytes
                graph |= {child} | sub.graph
                lost |= sub.missing
            memo[rel] = _Closure(tree, tree_bytes, doc_bytes, frozenset(graph), frozenset(lost))
        return memo[rel]

    return sizes, {rel: closure(rel) for rel in sizes}


def stats(root: str = ROOT) -> str:
    """The sizes of the descriptors and of their closures in ``Descriptor.uses`` (issue #695).

    All the numbers are bytes of tags, prose and formal, with no protobuf headers. The tree
    counts a descriptor one time for each place where ``uses`` holds it (the size on the
    wire); the graph counts each descriptor one time.
    """
    sizes, closures = _closures(root)
    out = []

    def graph_bytes(graph):
        return sum(sizes[r] for r in graph)

    def doc_bytes(graph):
        return sum(sizes[r] for r in graph if is_document(r))

    out.append("== 1. Each descriptor")
    out.append("file | formal | descriptor | closure tree/graph/documents | tree bytes | graph bytes"
               " | document bytes (graph) | ids with no file")
    for rel, c in closures.items():
        formal = len(read(os.path.join(root, rel)).formal)
        docs = sum(1 for r in c.graph if is_document(r))
        out.append(f"{rel} | {formal} | {sizes[rel]} | {c.tree}/{len(c.graph)}/{docs} | {c.tree_bytes}"
                   f" | {graph_bytes(c.graph)} | {doc_bytes(c.graph)} | {len(c.missing)}")

    out.append("")
    out.append("== 2. A typical service (the architecture + " + ", ".join(
        os.path.basename(r)[:-len(SUFFIX)] for r in TYPICAL_SERVICE) + ")")
    out.append("architecture | descriptors | own bytes | tree bytes (wire) | graph bytes"
               " | document bytes (graph) | documents | ids with no file")
    for arch in TYPICAL_ARCHITECTURES:
        roots = [r for r in (arch,) + TYPICAL_SERVICE if r in closures]
        own = sum(sizes[r] for r in roots)
        tree = own + sum(closures[r].tree_bytes for r in roots)
        graph = set(roots).union(*(closures[r].graph for r in roots))
        lost = set().union(*(closures[r].missing for r in roots))
        docs = [r for r in graph if is_document(r)]
        out.append(f"{os.path.basename(arch)[:-len(SUFFIX)]} | {len(graph)} | {own} | {tree}"
                   f" | {graph_bytes(graph)} | {doc_bytes(graph)} | {len(docs)} | {len(lost)}")

    out.append("")
    out.append("== 3a. The 10 largest descriptors by the bytes of their tree (own + closure)")
    ranked = sorted(closures, key=lambda r: (-(sizes[r] + closures[r].tree_bytes), r))
    for rel in ranked[:10]:
        out.append(f"{rel} | {sizes[rel] + closures[rel].tree_bytes}")
    out.append("")
    out.append("== 3b. The 10 largest documents")
    for rel in sorted((r for r in sizes if is_document(r)), key=lambda r: (-sizes[r], r))[:10]:
        out.append(f"{rel} | {sizes[rel]}")

    out.append("")
    out.append("== 4. Tree / graph (descriptors with a closure)")
    with_uses = [r for r, c in closures.items() if c.graph]
    total_tree = sum(closures[r].tree_bytes for r in with_uses)
    total_graph = sum(graph_bytes(closures[r].graph) for r in with_uses)
    if with_uses:
        worst = max(with_uses, key=lambda r: (closures[r].tree_bytes / graph_bytes(closures[r].graph), r))
        out.append(f"all | {total_tree} / {total_graph} = {total_tree / total_graph:.3f}")
        out.append(f"worst | {worst} | {closures[worst].tree_bytes} / "
                   f"{graph_bytes(closures[worst].graph)} = "
                   f"{closures[worst].tree_bytes / graph_bytes(closures[worst].graph):.3f}")
    else:
        out.append("no descriptor has a closure")
    return "\n".join(out)


def main() -> None:
    import sys

    if "--stats" in sys.argv:
        print(stats())
        return
    if "--cascade" in sys.argv:
        for rel in cascade():
            print(f"updated {rel}")
    path = os.path.join(ROOT, INDEX)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = text.replace(generated_part(text), render_generated())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    main()
