# The canonical encoding of a service

The id of a service is the hash of its bytes. So a service must have one set of bytes. This
document says which bytes. It is the rule for every message that has an id: the `Service`
and the `Filesystem` of its container.

The encoding is a small part of the protobuf wire format, with rules that give one
encoding for each value. Any protobuf library reads these bytes. A node does not need one to
write them or to check them (`src/utils/canonical_proto.py`).

## What is not in the encoding

The encoding says how one message is written. It does not say how a message is stored, read
fast, cut into blocks, compressed or sent. Those are other layers, and a node can change them
with no change in the id of any service:

| Layer | Where it is | Why it is not here |
|---|---|---|
| An index for a read of one field in O(1) | a file beside the document, made from its bytes | A node can drop it, rebuild it or use another one. It is checked against the bytes. |
| Blocks and their pointers | Bee-RPC | The id is the hash of the content with the blocks in place. A pointer is not part of the id. |
| Compression | the transport | The same bytes, with or without it. |
| The transport | gRPC | The same bytes on any stream. |

## A message

A message is a sequence of fields. A field is a key and a value.

```text
key   = a varint with the value  number * 8 + class
class = 0: the value is a varint
        2: the value is a varint L, then L bytes
```

Only the classes 0 and 2 are used. Any other class is refused. A varint is 1 to 10 bytes. It
holds 7 bits in each byte, the least significant first. The byte has its high bit set when
another byte follows.

| Type | Class | Value |
|---|---|---|
| `uint64`, `uint32`, `int32` (not negative), `bool` (0 or 1), `enum` (not negative) | 0 | the number |
| `string` | 2 | UTF-8 bytes |
| `bytes` | 2 | the bytes |
| a message | 2 | the fields of the message |
| a list of strings, bytes or messages | 2, once for each element | the element |
| a list of numbers | 2, once | the numbers as varints, one after the other (packed) |

## The rules

A writer follows all of these. A reader refuses bytes that break one.

1. **Order.** The fields are in ascending order of their number.
2. **Once.** A field that is not repeated is written at most once. The elements of a
   repeated field are one after the other.
3. **Short varints.** A varint has no extra bytes: its last byte is not zero, unless the
   whole value is zero.
4. **Unsigned.** A number is not negative and it fits its type. A negative number needs
   10 bytes and there is no use for it.
5. **Defaults.** A field that has no presence (a scalar, string or bytes that is not
   `optional`) is not written when it has its default value: 0, empty or false. A field that
   has presence (a message, an `optional` field, a member of a `oneof`) is written if and only
   if it is set, also when it is empty. An empty file is `file = ""` in a `oneof`: it is
   written, and it is not the same as a directory.
6. **Packed lists.** A list of numbers is one field, packed, and it is not written when it is
   empty. There is no list of numbers in a `Service` today.
7. **UTF-8.** A string is valid UTF-8.
8. **One member.** At most one member of a `oneof` is present.
9. **Types.** A message of an id has no `map`, group, `float`, `double`, signed number
   (`sint*`, `int64`), or fixed-width number (`fixed*`, `sfixed*`). A test reads the schema
   of the `Service` and of the `Filesystem` and fails if such a field enters it
   (`tests/test_canonical_proto.py`). A key and value list is a list of messages.

## Fields that the reader does not know

A node that has an older schema can get a service from a newer writer. It skips the field
that it does not know: it still checks the order of the keys, the classes and the varints, and
it cannot check the rules that need the type. It does not write the message again. A node that
has the id forwards the bytes as they came, so the id does not change.

## Numbers of fields

A number of a field is never changed after a release: the key is part of the bytes. A field
that is removed has its number in a `reserved` line. A new field takes the next number. The one
gap that exists is `Sysresources.benchmark = 99`. It costs 1 byte more in its key. It stays as
it is, because a new number would change the id of each service that declares a benchmark.

## Where the node checks it

| Where | What | Setting |
|---|---|---|
| The packer | the specification as stored, and the filesystem block | `misc.CANONICAL_SERVICE` |
| `StartService` | the specification, and that the content hashes to the id of the request | the hash is always checked |
| `GetService` (the node asks a peer) | the specification, after the id | `misc.CANONICAL_SERVICE` |
| `nodo import` | the specification | `misc.CANONICAL_SERVICE` |

What is checked is the specification as it is stored: the message with the pointers of the
blocks, and not the content of the blocks. The content is bytes that the hash covers. A
filesystem that a node receives is a block, so only the packer checks its encoding. Two nodes
that hold the same id hold the same bytes, so a service that is not canonical is refused by
each of them.

## Why not a format of its own

A format with a table of offsets before the content, or a footer, was studied (SeSe). Such a
table is part of the bytes, so it is part of the id, and a later change of the table would
need a second format. The index is a separate file instead (see above). The protobuf wire format
is stable, and it has a reader in each language that Celaut uses. A format of its own would
change every id, stop Bee-RPC, and need a code generator for each language. The classes 1, 3,
4 and 5 of a key stay unused. They are not a choice that a user of this encoding makes.
