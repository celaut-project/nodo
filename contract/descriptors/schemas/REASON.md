# Schemas

A type of a message, with the source files that define it, so that a reader that did not
get the bytes from their writer reads them with the meaning that the writer gave
(REASON 1.8).

Class: `schema`.

## Places

- `Service.schema` (field 1 of `Service`): the schema of the service. Its id covers it.
- `Signed.Body.context`: the schema of the signed content. The signature covers it.

## Value

None: a schema is a `Descriptor`. A change to a byte of a source file, also of a
comment, is a new schema.

## The formal must give

- `type`: the full name of the type, for example `celaut.Service`.
- `root`: the path of the file that defines the type, as an `import` writes it, for
  example `celaut.proto`.
- `files`: the documents of the source files: the file of `root`, and each file that
  it imports, at all depths. Each item is the path as an `import` writes it, a space,
  and the id of the document of the file. Items are separated by `,` and sorted
  (rule 3.2). So a reader that finds `import "buffer.proto"` knows which document it
  is.
- `language`: the id of the document of the language of the files (for example the
  specification of the language of protobuf, version 3), and of the encoding of the
  messages on the wire.

The files are documents of `../documents/` (rule 3.13 of `../REASON.md`): their exact
bytes, with their comments. The comments give the meaning of each field, so they are a
part of the schema.

## Notes

- Write one schema for each type that a place needs (`celaut.Service`, `celaut.Peer`,
  ...), for each version of the files. Write the first ones when `celaut.proto` is
  frozen: until then, each change makes new documents and new schemas.
- The gateway layer of a node names the same documents for its messages and its RPCs.
