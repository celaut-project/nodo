# How to write a descriptor

This file has the rules to write the descriptors that `celaut.proto` uses. It is for a
person or an agent that must add a descriptor for nodo. Read `contract/REASON.md` first:
it has the rules of the contract. This file applies them to the files of this folder.

## 1. What a descriptor is

**Rule 1.1. A descriptor is one thing of the second level.** The contract
(`celaut.proto`) holds mechanisms. A descriptor defines one thing that the contract does
not define: a resource, a benchmark, a price, a setting, a ledger, an architecture, a
protocol, a schema (REASON 1.2). A new thing is a new descriptor, never a change to the
contract.

**Rule 1.2. A file is one `celaut.Descriptor`.** A descriptor has three parts:

- `tags`: names, for a reader;
- `prose`: the text, for an implementer;
- `formal`: the bytes that nodes compare;
- `uses`: the descriptors that the formal names by their ids (REASON 2.9).

The file holds the first three. It does not hold `uses`: a writer builds it from the
ids of the formal, with the files of this folder (`uses` in
`src/utils/descriptor_file.py`).

Every entry of the contract holds a `Descriptor`: a `Policy`, a `Quantity`, a `Rate` and
a `Limit` are a `Descriptor` plus a value (REASON 2.1). The value (and the scope of a
rate or a limit) is of each use, so it is never in the file.

**Rule 1.3. Only the formal is the identity.** Nodes compare two descriptors by the
exact bytes of their formals (REASON 2.2). A change to the tags or to the prose does
not change the identity. A change to one byte of the formal makes a new descriptor.

**Rule 1.4. A published formal never changes.** A new meaning, a new version or a fix
is a new file with a new formal (REASON 2.5, 2.6). Do not edit the formal of a file that
is in a commit of the branch `dev`.

## 2. The file

**Rule 2.1. The path.** A file is `contract/descriptors/<folder>/<name>.descriptor`. The
name is the value of the line `name=` of its formal (rule 3.3). The folders follow the
uses: one folder for one place of `celaut.proto`, or for a group of places that take
the same descriptors (section 4). Each folder has a `REASON.md` that says what the
group is for, the places, the position of the list for each reader, the form of the
value, and what nodo does with it. A folder that holds descriptors gives its class in
a line `Class: `<class>`.`. A folder that only holds folders has no such line.

**Rule 2.2. The format.** UTF-8, with LF line ends only. The parts, in this order:

1. The first line: `tags:` and the tags, separated by commas. The line can have no
   tags. The first tag is the name.
2. The prose: all the lines after the first line and before the marker line. The
   empty lines at its start and at its end are not a part of it.
3. The marker line: the first line `=== formal`, alone on its line.
4. The formal: all the bytes after the marker line, up to the end of the file,
   without the last newline. The file ends with a newline. The formal of a document
   (rule 3.13) can hold any byte, also newlines at its end and the marker line.

Example:

```
tags: memory, ram

The memory of one instance, in bytes. It is a total for the instance, not per core.
The node reserves it and enforces it.

=== formal
class=resource
measure=the most memory that the processes of the instance can hold at one time
name=memory
unit=byte
```

`src/utils/descriptor_file.py` reads this format. It is the reference: if this text and
the reader do not agree, report it.

**Rule 2.3. The prose goes on the wire.** A writer copies the tags and the prose of the
file into the `Descriptor` that it sends, and adds the `uses`. They are also in the bytes
of a `Service` that holds the descriptor, so a change to the prose changes the id of a
service packed after the change. Send the full prose: the reader of the test of the
probe (rule 3.11) has no other text.

## 3. The formal

**Rule 3.1. The form.** The formal is lines of `key=value` (except the formal of a
document, rule 3.13):

- one line for each key, with no repeated key;
- the lines sorted by the bytes of the key, ascending;
- a key is ASCII letters, digits, `_`, `-`, `.` and `+`; keys are compared by their bytes,
  so `a` and `A` are two keys (a key can hold a name of the contract, such as
  `schema.buffer.Buffer`);
- no space around `=`; the value is the rest of the line, and it can contain `=`;
- no empty line, and no newline at the end.

**Rule 3.2. A list in a value** is items separated by `,` with no space. Sort the items
by their bytes, unless the formal says that the order has a meaning.

**Rule 3.3. Two keys are always necessary** (except in a document, rule 3.13).

- `class=`: the class of the descriptor (section 4). It is a property of the thing
  (REASON 2.4): the memory as a resource and the price of the memory are two things.
- `name=`: a short name, unique in its class. Without it, two descriptors with the same
  keys (for example two settings with `values=off,on`) are one formal.

**Rule 3.4. The formal is complete.** A second implementer that reads only the formal
and the prose must get the same behavior as nodo. Put in the formal each fact that two
nodes must agree on: the unit, the form of the value, the default, the places where it
is valid, how a node measures or checks it. Put the explanation and the examples in the
prose.

**Rule 3.5. A property goes in the formal; a behavior does not** (REASON 2.4). "Memory
is in bytes and is a total" is a property. "Release the memory when the instance
hibernates" is a behavior: it is a setting of its own, in another descriptor.

**Rule 3.6. Units.** The formal of a thing with a number gives its unit (REASON 2.3). The
values of the contract are integers: select a unit that makes the values integers (for
example `unit=core/1000` for the CPU, not a fraction of a core). No floating-point
values. A ratio is two integers (REASON 6.1).

**Rule 3.7. A reference to another formal is its id.** A descriptor that is about
another descriptor (a price of a resource, a benchmark of a device) names it by its id,
`<type>:<digest>` (REASON 3.5, 6.3), never by its name. The id is in `INDEX.md`.
Example: `of=<id of the formal of memory>`. If the formal that you name changes, your
formal changes too: it is a new descriptor.

**Rule 3.8. A replacement names the old formal.** A descriptor that replaces another has
`replaces=<id of the old formal>`, and its formal says that a list with both is not
valid (REASON 2.6). Give it a new name, for example with `-v2` at the end.

**Rule 3.9. A hash algorithm is always named by its type.** The type is the digest of
the empty input, in lowercase hexadecimal (REASON 6.3, 6.4), for example
`hash.type=e3b0c442...b855` for SHA-256. Never name a hash algorithm by a name such as
"SHA-256" or "BLAKE2b", in the formal or in an id. A variant with another digest size,
a key, a salt or a personalization has another empty digest, so the type also fixes the
variant. A digest in a formal is an id, `<type>:<digest>`, also when another format
writes it in another way (for example the digest of a container image).

**Rule 3.10. No implementation in a formal.** No path of nodo, no name of a module, no key
of the configuration of nodo, no name of this project. A formal is for all
implementations.

**Rule 3.11. Each term of a formal resolves to a base.** A formal is for the reader of
the test of the probe (REASON 2.9): a reader that knows only ASD-STE100, `celaut.proto`
and the encoding of protobuf, and has no network. Each term of a formal is one of these
three:

1. a word of ASD-STE100 with its approved meaning, or a name that `celaut.proto`
   defines (a message, a field, an RPC): the reader of a descriptor reads the
   contract first;
2. a number with a unit of the base: bit, byte (8 bits) or second. Write a larger unit
   as a multiple (1073741824 bytes, 3600 seconds);
3. the id of another descriptor. A document is a descriptor (rule 3.13): its id is the
   digest of the bytes of the document. Give the part of the document that applies
   (for example a section).

A technical name that is none of these (for example "x86-64-v1", "UDP", "virtio
console") becomes a descriptor or a document. The test: **can that reader find the
meaning of each term of the formal in `uses`, and build a machine that does what the
formal says?** If not, the formal is not complete (rule 3.4): two implementers can read
the same bytes in two ways, and nothing finds it. A test that a machine passes (for
example bits of CPUID) identifies the machine, but does not tell how to build it: the
formal also names the document that defines the thing.

**Rule 3.12. A concept or a document.** Make a descriptor of `concepts/` for a term only
when the term has variants (so the variant is one leaf, and a change of it is a change
of one id), or when more than one descriptor uses it. For any other term, name the
document directly. A document is named only by the hash of its exact bytes. Its public
name (for example "RFC 768") and its version go in its tags and its prose, and its
license notice goes in its prose (rule 3.13).

The descriptors make a graph: a formal names its parts by their ids, and the parts can
be shared. It is not the tree of the layers of an address (REASON 2.7). In that tree,
the children of a layer are alternatives. In a formal, the parts are all true together.
Nodes still compare only the bytes of the formal that is in the place of the contract;
its parts change its bytes, because their ids are in it.

**Rule 3.13. A document is a descriptor whose formal is the document.** The formal is
the exact bytes of the document: a specification, a file of the source of a program, a
program. It is not `key=value` lines, and it has no `class=` and no `name=` line. The
reader knows that it is a document from the formal that names it ("the rows of the
document <id>"). A document has no `uses`: its bytes are not read as a formal. If a
document needs another document, the formal that names the first one also names the
second one.

Any document can be a document, with any license. The prose of a document gives its
license notice: who holds the copyright, and the license, as the document gives them.
The contract does not decide if a party can keep or send a document: each node and each
operator decides it from the notice.

## 4. The classes and the folders

The class says in which places of the contract the descriptor is valid. The contract
says, for each place, if the list is a requirement or an announcement (REASON 3.2). A
node refuses a descriptor in a place of another class.

The folders, by use:

| Folder | Class | Places |
| --- | --- | --- |
| `architectures/` | architecture | `Container.architecture`, `Peer.resources` |
| `architectures/devices/` | architecture | `Device.architecture`, `Peer.devices` |
| `quantities/resources/` | resource | every list of resources |
| `quantities/benchmarks/` | benchmark | every list of benchmarks |
| `policies/container_init/` | setting | `Container.Init.policies` |
| `policies/filesystem_root/` | setting | `Container.Filesystem.policies` of the root filesystem |
| `policies/filesystem_entry/` | setting | `Container.Filesystem.ItemBranch.policies` |
| `policies/api_slot/` | setting | `Service.Api.Slot.policies` |
| `policies/network/` | setting | `Service.Network.policies` |
| `policies/device/` | setting | `Device.policies` |
| `policies/configuration/` | setting | `Configuration.policies` |
| `facts/` | fact | `ArchitectureResources.facts`, `StartReceipt.facts` |
| `terms/conditions/` | condition | `conditions` of `EstimatedCost` and `Peer` |
| `terms/promises/` | promise | `promises` of `EstimatedCost` and `Peer` |
| `prices/node/` | rate | `Peer.rates` |
| `prices/service/` | rate | `Service.Api.Slot.rates` |
| `limits/` | limit | `Peer.limits` |
| `networks/` | domain | `Service.Network.domain` |
| `dependency_sources/` | source | `Workload.Dependency.sources` |
| `protocols/address/` | layer | `Layer.protocol`, `Service.Network.protocol_stack` |
| `protocols/node_gateway/` | layer | `Container.node_protocol_stack` |
| `contracts/payment/` | payment-protocol | `Contract.protocol` |
| `ledgers/` | ledger | `Contract.ledger` |
| `signatures/schemes/` | signature-scheme | `Signed.Body.scheme` |
| `schemas/` | schema | `Service.schema`, `Signed.Body.context` |
| `environment_formats/` | format | `DescriptorKeyValue.value` of `Container.environment_variables` |
| `proof_of_work/` | proof-of-work | `PoWRequired.descriptor` |
| `concepts/` | concept | no place: only in the formal of another descriptor, by its id |
| `documents/` | document | no place: only in the formal of another descriptor, by its id |

To add a folder, add a row here and write its `REASON.md`. A folder is a use, not a
kind of thing: the memory as a resource and the price of the memory are in two
folders.

What the formal of each class must give:

| Class | Entry | Places in `celaut.proto` | The formal must give |
| --- | --- | --- | --- |
| `resource` | `Quantity` | `Resources.at_init`, `Resources.at_most`, `ArchitectureResources.resources`, `StartReceipt.reserved`, `InstanceState.resources`, the resources of a request | `unit`, and how the node counts and enforces it (`measure`) |
| `benchmark` | `Quantity` | `Resources.benchmarks`, `ArchitectureResources.benchmarks` | `unit`, the statistic (`stat`: a mean or a percentile, with its window), and how the node measures it (`method`) |
| `rate` | `Rate` | `Peer.rates`, `Api.Slot.rates` | what is charged (`of`: the id of a resource, or an action), the unit of the base, and the meaning of the scope (`scope`, or `scope=none`) |
| `limit` | `Limit` | `Peer.limits` | `unit`, for whom (`per`: one client, or all), and the meaning of the scope |
| `setting` | `Policy` | the `policies` of `Api.Slot`, `Filesystem`, `ItemBranch`, `Init`, `Network`, `Device`, `Configuration` | `place` (the fields where it is valid), the form of the value, and the default when the setting is absent |
| `condition` | `Policy` | `conditions` of `EstimatedCost` and `Peer` | the form of the value. A condition binds the caller |
| `promise` | `Policy` | `promises` of `EstimatedCost` and `Peer` | the form of the value. A promise binds the node |
| `fact` | `Policy` | `ArchitectureResources.facts`, `StartReceipt.facts` | `place`, and the form of the value |
| `source` | `Policy` | `Dependency.sources` | the form of the value (the location) |
| `domain` | `Policy` | `Network.domain` | the form of the value, and how a node finds the members |
| `architecture` | `Descriptor` | `Container.architecture`, `Device.architecture`, `ArchitectureResources.architecture` | what the software programs against |
| `layer` | `Descriptor` | `Layer.protocol`, `Container.node_protocol_stack`, `Network.protocol_stack` | the protocol, and the value that selects it in its parent layer (REASON 2.7) |
| `schema` | `Descriptor` | `Service.schema`, `Signed.Body.context` | the structure of the type, with the name of each field |
| `signature-scheme` | `Descriptor` | `Signed.Body.scheme` | the algorithm, the key form and the signature form |
| `ledger` | `Descriptor` | `Contract.ledger` | the network, the units, and the keys of a proof |
| `payment-protocol` | `Descriptor` | `Contract.protocol` | the keys of `Contract.xattrs`, and the way to pay |
| `format` | `Descriptor` | `DescriptorKeyValue.value` (environment variables) | the form of the bytes |
| `proof-of-work` | `Descriptor` | `PoWRequired.descriptor` | the rule of the work, and the unit of the difficulty |
| `concept` | (none) | no place: only in another formal | what the term is, in terms of the base (rule 3.11), and its variant |
| `document` | (none) | no place: only in another formal | the exact bytes of the document (rule 3.13) |

To add a class, add a row here first. A class is a kind of place, not a kind of thing.
The test reads the classes from the first column of this table.

**Rule 4.1. A resource adds, a benchmark compares.** A resource is consumed: the node
adds the values of its instances and keeps the sum within its capacity. A benchmark is
a score: the node compares it with "greater than or equal" and never adds it. Both
compare with "more is better": write a thing that is better when it is less as its
inverse (REASON 3.3). A requirement that is not "greater than or equal" (an exact value,
a feature that is present or not) is a `setting`, not a `Quantity`.

**Rule 4.2. A condition or a promise: the test.** If the caller is better off when the
term is absent, it is a condition. If the caller is equal or worse off when it is
absent, it is a promise (REASON 3.2).

**Rule 4.3. A setting in a requirement fails closed.** A node that does not know a
setting refuses the service. So give each setting a default for its absence that is
safe on an old node, and do not make a service need a setting without a reason.

## 5. The value

**Rule 5.1. The formal defines the value.** For a `Policy`, the formal gives the form of
`value` and what an empty value means. For a `Quantity` or a `Limit`, the value is an
integer in the unit of the formal. For a `Rate`, it is an `Amount` in MU for one unit
of the formal.

**Rule 5.2. Text values.** Use UTF-8. Write an integer in base 10, with no sign and no
leading zero. Write a list as in rule 3.2. Write a group of keys as `key=value` lines,
sorted by key, as the setting `share` does. A binary value is valid only when the formal
says so, and gives its form.

**Rule 5.3. One thing with many members: a formal and a value, where the place has a
value.** For example one fact "PCI model" with the value `10de:2330`, not one descriptor
for each model. One network kind with the chain in the value, not one descriptor for each
chain. Only a place with no value (a `Descriptor`: an architecture, a layer) needs one
descriptor for each member. Then the members share their keys, and only the key that
names the member changes.

## 6. Ids, index and tests

**Rule 6.1. The id.** The id of a descriptor is the id of its formal: `<type>:<digest>`,
with the type as the digest of the empty input (REASON 6.3). Nodo uses blake2b-256.
Another algorithm gives another id for the same bytes, and both are valid.

**Rule 6.2. The index.** `INDEX.md` has two parts. The first part lists each file with
its tags and its id: it is generated, between two marker lines, so do not edit it.
After a change, run:

```
python -m src.utils.descriptor_file
```

The second part is the list of the descriptors that nodo needs and that have no file
yet, by folder, with where nodo implements each one today. When you write one, remove
it from that list.

**Rule 6.3. The tests.** `tests/test_descriptor_files.py` checks that each file follows
sections 2 and 3, that it is in a folder with a `REASON.md` and a class, that its
`class=` line is the class of its folder, that the generated part of `INDEX.md` is
current, and that each id in a formal is the digest of a formal of
`contract/descriptors/`, so that `uses` can hold it (REASON 2.9). When the code of nodo uses a descriptor, the code reads the
file, or holds the same bytes and a test pins them to the file.

**Rule 6.4. The legacy descriptors.** A formal that nodo sends today (on the wire, or in
the databases of the nodes that run today) is copied byte for byte, with no `class=`
and no `name=` line: the settings of `policies/filesystem_root/`,
`policies/filesystem_entry/` and `policies/api_slot/`, the payment protocols, the layers
of an address, the ledgers and the signature scheme. They get the two lines when the
code follows the new contract (stage 7 of `contract/PENDING.md`). The test knows them by
their paths (the set `LEGACY`), and `tests/test_descriptor_legacy_formals.py` pins each
one to the bytes that the code builds.

## 7. The process

Do these steps for each new descriptor:

1. Find the place in `celaut.proto` where the descriptor goes. If there is no place,
   stop: a new place is a change to the contract (`contract/REASON.md`, section 8).
2. Read the comment of that place. Find if the list is a requirement or an
   announcement for each reader, and what an absent entry means.
3. Select the folder and its class (section 4). Read the `REASON.md` of the folder.
   Check rule 4.1 or rule 4.2 if they apply.
4. Look in `INDEX.md` and in the folder for a descriptor of the same thing. Use it if
   it exists. Do not make a second formal for one thing.
5. Write the formal (section 3): `class`, `name`, the unit, the form of the value, the
   default, the references by id. Ask: can a second implementer get the same behavior
   from it?
6. Write the tags and the prose. In the prose, say what the thing is, the form of the
   value with an example, what an absent entry means, and who is bound.
7. Save the file at `<folder>/<name>.descriptor`. Remove the item from the second part
   of `INDEX.md`.
8. Run `python -m src.utils.descriptor_file`, then the tests.

## 8. Signs that a descriptor is wrong

- Two descriptors for one thing, or one descriptor for two things.
- A number with no unit, or a unit that gives fractions.
- A behavior in the formal of a property.
- A name or a path of nodo in the formal.
- A reference to another descriptor by its name or its tags, not by its id.
- A hash algorithm named by a name, not by the digest of the empty input.
- A term that is not a word of ASD-STE100, a name of `celaut.proto`, a number with a
  unit of the base, or an id (rule 3.11).
- A formal that only nodo can read: its meaning is in the code, not in the text.
- A term that names a thing of our world (a brand, a product, a program, "the
  behavior of Linux") with no document in `uses` that defines it (rule 3.11).
- An id of bytes that no file of this folder holds: `uses` cannot hold them.
- A descriptor for each member of a group, in a place that has a value.
