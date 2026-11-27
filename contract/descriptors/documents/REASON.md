# Documents

A document that a formal needs: a specification (an RFC, a manual of an
instruction set, an ABI), a file of the source of a program, a program. The document
gives the meaning of a technical name that the base of rule 3.11 of `../REASON.md` does
not have.

Class: `document`.

A document has no `class=` line: the class is of the folder.

## Places

None in `celaut.proto`. A document is only in the formal of another descriptor, by its
id. The formal that names it also says which part of the document applies (for
example a section).

## Value

None.

## The formal must give

The exact bytes of the document, and nothing more (rule 3.13 of `../REASON.md`). The
formal has no `class=` and no `name=` line. The id of the document is the digest of
these bytes. The file name is the first tag.

The public name of the document (for example "RFC 768"), its version and where it
comes from go in the tags and the prose. A document can have any license. The prose
ends with its license notice: a paragraph that starts with `License notice:` and gives
who holds the copyright and the license, as the document gives them. The document goes
on the wire, in `Descriptor.uses`: each node and each operator decides from the notice
if it can keep or send it. If the document changes (a new version, a correction), it is
a new file. Copy the bytes with
no change: a document that ends with a newline keeps it, and the file then ends with
two newlines.
