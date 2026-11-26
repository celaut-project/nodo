# Schemas

The structure of a message, with the name of each field, so that a reader that did not
get the bytes from their writer reads them with the meaning that the writer gave
(REASON 1.8).

Class: `schema`.

## Places

- `Service.schema` (field 1 of `Service`): the schema of the service. Its id covers it.
- `Signed.Body.context`: the schema of the signed content. The signature covers it.

## Value

None: a schema is a `Descriptor`. A comment does not change a schema. A change to the
structure of a type, or of a message that it contains, is a new schema.

## Notes

The format of a schema is still to decide (`protos/PENDING.md`, question 7). The gateway
layer of a node uses the same format.
