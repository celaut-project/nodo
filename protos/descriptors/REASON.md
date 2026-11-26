# How to write a descriptor

This file has the rules to write the descriptors that `celaut.proto` uses. It is for a
person or an agent that must add a descriptor for nodo. Read `protos/REASON.md` first:
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
- `formal`: the bytes that nodes compare.

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

**Rule 2.1. The path.** A file is `protos/descriptors/<class>/<name>.descriptor`. The
class and the name are the values of the lines `class=` and `name=` of its formal (rule
3.3). The class folder has a `README.md` that lists its descriptors.

**Rule 2.2. The format.** UTF-8, with LF line ends only. The parts, in this order:

1. The first line: `tags:` and the tags, separated by commas. The line can have no
   tags. The first tag is the name.
2. The prose: all the lines after the first line and before the marker line. The
   empty lines at its start and at its end are not a part of it.
3. The marker line: `=== formal`, alone on its line. The file has it one time.
4. The formal: all the bytes after the marker line, up to the end of the file,
   without the last newline. The file ends with exactly one newline.

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
file into the `Descriptor` that it sends. They are also in the bytes of a `Service` that
holds the descriptor, so a change to the prose changes the id of a service packed
after the change. A writer can send a short prose or none: a reader finds the full text
by the id of the formal (rule 6.1).

## 3. The formal

**Rule 3.1. The form.** The formal is lines of `key=value`:

- one line for each key, with no repeated key;
- the lines sorted by the bytes of the key, ascending;
- a key is lowercase ASCII letters, digits, `_`, `-` and `.`;
- no space around `=`; the value is the rest of the line, and it can contain `=`;
- no empty line, and no newline at the end.

**Rule 3.2. A list in a value** is items separated by `,` with no space. Sort the items
by their bytes, unless the formal says that the order has a meaning.

**Rule 3.3. Two keys are always necessary.**

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

**Rule 3.9. No implementation in a formal.** No path of nodo, no name of a module, no key
of the configuration of nodo, no name of this project. A formal is for all
implementations.

## 4. The classes

The class says in which places of the contract the descriptor is valid. The contract
says, for each place, if the list is a requirement or an announcement (REASON 3.2). A
node refuses a descriptor in a place of another class.

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

To add a class, add a row here first. A class is a kind of place, not a kind of thing.

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

**Rule 6.2. The index.** `INDEX.md` lists each file with its tags and its id. Do not edit
it. After a change, run:

```
python -m src.utils.descriptor_file
```

**Rule 6.3. The tests.** `tests/test_descriptor_files.py` checks that each file follows
sections 2 and 3, that `INDEX.md` is current, and that each reference (rule 3.7) names
a formal of this folder. When the code of nodo uses a descriptor, the code reads the
file, or holds the same bytes and a test pins them to the file.

**Rule 6.4. The descriptors before this file.** The files of `policies/` and
`payment_protocols/` have no `class=` and no `name=` line. Their bytes are on the wire
and in the databases of the nodes that run today, so they stay as they are. They get
the two lines when the code follows the new contract (stage 7 of `protos/PENDING.md`).
The test knows them by their paths.

## 7. The process

Do these steps for each new descriptor:

1. Find the place in `celaut.proto` where the descriptor goes. If there is no place,
   stop: a new place is a change to the contract (`protos/REASON.md`, section 8).
2. Read the comment of that place. Find if the list is a requirement or an
   announcement for each reader, and what an absent entry means.
3. Select the class (section 4). Check rule 4.1 or rule 4.2 if they apply.
4. Look in `INDEX.md` and in the folder of the class for a descriptor of the same thing.
   Use it if it exists. Do not make a second formal for one thing.
5. Write the formal (section 3): `class`, `name`, the unit, the form of the value, the
   default, the references by id. Ask: can a second implementer get the same behavior
   from it?
6. Write the tags and the prose. In the prose, say what the thing is, the form of the
   value with an example, what an absent entry means, and who is bound.
7. Save the file at `<class>/<name>.descriptor`. Add a row to the `README.md` of the
   class.
8. Run `python -m src.utils.descriptor_file`, then the tests.

## 8. Signs that a descriptor is wrong

- Two descriptors for one thing, or one descriptor for two things.
- A number with no unit, or a unit that gives fractions.
- A behavior in the formal of a property.
- A name or a path of nodo in the formal.
- A reference to another descriptor by its name or its tags, not by its id.
- A formal that only nodo can read: its meaning is in the code, not in the text.
- A descriptor for each member of a group, in a place that has a value.
