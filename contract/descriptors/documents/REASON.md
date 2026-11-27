# Documents

An external document that a formal needs: a specification (an RFC, a manual of an
instruction set, an ABI), a file of the source of a program, a program. The document
gives the meaning of a technical name that the base of rule 3.11 of `../REASON.md` does
not have.

Class: `document`.

## Places

None in `celaut.proto`. A document is only in the formal of another descriptor, by its
id. The formal that names it also says which part of the document applies (for
example a section).

## Value

None.

## The formal must give

- `digest`: the id `<type>:<digest>` of the exact bytes of the document. The type is
  the digest of the empty input (rule 3.9 of `../REASON.md`).
- `bytes`: the number of bytes of the document.

Only these bytes identify the document. Its public name (for example "RFC 768"), its
version and where to get it go in the prose. If the document changes (a new version, a
correction), it is a new descriptor. Get the document, compute its digest, and check
the digest before you write the file.
