# How to reason about celaut.proto

This file gives the rules for reasoning about a change to `celaut.proto`. The goal is
the simplest contract that never needs an update. Use these rules before you add,
change or remove a field, a message or an enum value. Use them also to judge a design
that someone else proposes, and to judge your own.

The two goals pull in different directions. A contract with fewer fields is simpler.
A contract with more extension points needs fewer updates. The rules below tell where
each goal wins.

## 1. The two levels

The protocol has two levels:

1. **The contract** (`celaut.proto`). It is static. All nodes must agree on it
   exactly, and a change to it is expensive.
2. **The formal definitions** above the contract: policies, resources, benchmarks,
   rates, limits, architectures, APIs, protocols. A formal is a set of bytes that a
   document defines. Nodes can share, negotiate or ignore formals. Formals can grow
   with no change to the contract.

**Rule 1.1. Name the level first.** For each question, first decide which level it
belongs to. Most bad designs come from a question that is answered at the wrong
level.

**Rule 1.2. The contract holds mechanisms, not content.** The contract says how to
name, compare, sort, refuse, sign, hash and transport things. It does not say which
things exist. A list of the things that exist is a catalog, and a catalog grows.

**Rule 1.3. The interoperability test.** A rule belongs to the contract only if two
nodes cannot work together when they disagree on it: the wire format, the identity
of a formal, the comparison rule, the refusal rule, the signature. If two honest
nodes can want different behavior, the rule is a formal.

**Rule 1.4. The future test.** Ask: "Can a new case of this appear in the future?" If
yes, it is a formal, not a field and not an enum value. Memory, CPU, a benchmark, a
price, a signature scheme: each one is one case of an open set. A kind of network domain
is one more. So `ResolveNetwork` takes any `Service.Network`, not one kind of it: the
formal of the domain gives the kind. A caller that must know the kind first does the
resolution itself.

Some things pass both tests: two nodes cannot work together if they disagree on them,
and a new case can appear (a signature scheme, a hash algorithm, a transport, a
payment protocol). So there are three classes, not two:

1. A constant of the contract.
2. A formal on which two nodes must agree to do one thing together.
3. A formal that a node can ignore (for example a benchmark that it did not measure).

For the second class, the contract holds a place for the formal, never the thing. Two
nodes then agree by the exact bytes of that formal. They work together for that thing
only when the bytes are equal, or when one of them finds an implementation (rule 1.6).

**Rule 1.5. The elemental test.** Some concepts are not cases of an open set. They
are part of what the contract describes, and the contract cannot describe its basic
objects without them. A concept is elemental only if you can write it as one
statement about the basic objects of the contract, and its negation, with no third
case. An instance is a process that runs on a defined container, so "the instance has
a process" and "the instance has no process" (hibernated) are elemental.

Keep an elemental concept in the contract, once. A value of the contract for an
elemental concept says only that statement. If the value also says what the node
does, when, or at what cost, that part is a formal. For example, the three outcomes
of a running instance when the window of a node closes (keep the process, lose it,
end) are elemental, but "refuse new children until the window opens" or "wake it when
the window opens" are behaviors, so they are formals. Ask: "Is this a new kind of
thing, or a new way to do a thing that the contract already has?" Only the first can
be a candidate for the contract.

**Rule 1.6. An unknown formal is a question, not an end.** A node does not need to
know every formal. The `tags`, the `prose` and the `formal` bytes of an unknown entry
are enough to look for a celaut service that knows it. Then the node can run that
service and use it. So the network adapts by itself: a node learns a new formal when
it meets it, with no change to the contract and no new release of the node.

Example: a node speaks only one signature scheme. It meets a peer with another scheme
(the scheme of the signature of its `Peer`). It cannot verify the peer by itself. But the scheme is a
formal, so the node can look for a celaut service that verifies signatures of that
scheme, run it, and verify the peer. If the scheme were a field or an enum value, the
node could do nothing: the contract would have to change first.

Consequences:

- Every formal must carry enough to find an implementation: tags and prose for a
  person, and the exact bytes as the search key for a node.
- A thing that a service can do for a node (verify, convert, measure, transport) is
  a good candidate for a formal: the network can supply it.
- The trust moves to the binding "this service implements this formal". A party
  (a client or a node) uses a service for a formal that it does not know only when
  it trusts a binding of the exact bytes of the formal to the content hash of the
  service. Without a trusted binding, the old answer stays: a node never accepts a
  signature that nobody verified.
- The binding is not in the contract: two nodes work together with no agreement on
  how each one finds a service (rule 1.3). Each party selects its sources. For
  example: the list of its operator, or a registry of objectives, where a coverage
  binds a service to an objective and benchmarks and results measure it. The usual
  flow is on the client: it reads the registry, selects one service, and asks a node
  to run it by its hash. A node needs a source of its own only when it must use a
  service itself (for example, to verify a peer with an unknown signature scheme).
- The contract gives only what the search needs: the exact bytes of the formal as
  the key, a refusal that names the formal (rule 3.7), and a service id that also
  covers the schema of the service (rule 1.8), so that nobody can read a binding with
  another meaning.
- The contract does not know the author of a service: a service is a hash, and
  nobody signs it. So the defence is not a rule about who publishes. It is the
  reputation that each publisher puts at risk, and the results that others measure.
  A party that gains from the answer can publish with another identity.
- The node that runs the service is also in the chain of trust: it can lie about the
  answer. A party runs the service on itself, or on a node that it trusts.
- The rule has a limit. A service can verify, convert or measure, because these are
  functions with an input and an output. A service cannot enforce a policy inside
  the hypervisor of the node.

**Rule 1.7. The contract is complete; an implementation is a subset.** The contract
can define more than one node implements. A node that does not implement a part of
the contract does not announce it, or refuses with the cause (rule 3.7). What the
contract defines and a node does not implement is not a defect of that node. So a
design can close the contract now and leave the work of the node for later. Do not
make the node do more work only because the contract allows it.

**Rule 1.8. Variants, not versions.** Nobody owns the numbering of the contract.
Independent variants of it can exist, and they cannot agree in advance to never
reuse a field number. So the defence against a wrong reading is not a discipline of
field numbers. It is this:

- Between two nodes, each address announces its layers, and the layer
  `celaut-gateway` includes the schema of its messages. Two nodes compare the layers
  before they talk (rule 2.5).
- A node that reads a message checks that it makes sense, and refuses a field number
  that it does not know in a requirement (rule 3.6).
- A message that can reach a reader by another way (by its id, in a file, from a third
  node, as evidence) holds its schema, as a `Descriptor`. A reader compares the
  formal of that schema with its own schema before it reads the message.

Only two places hold a schema. A `Service` holds it in its field 1: it is the only
message whose id is a hash of its bytes, so its id covers what the bytes mean. Every
other message that a third party reads is signed, so `Signed.Body.context` holds the
schema of the content, and the signature covers it. So the signature covers what the
content means, and the context also keeps a signature for one use from another use. A
message that goes directly between two parties needs no schema: the layers of the
address give it.

The schema is a formal like any other (rule 2.2). It names the type, and the source
files that define it: the `.proto` files, with their comments, as documents in its
`uses` (rule 2.9), and the document of the language of those files. So the schema
gives the structure of each message, the name of each field, and what each field
means. A comment is a part of the schema. Nobody can know if two different comments
are a correction of one meaning or two meanings: only equal bytes show that two schemas
speak of the same thing. A reader can decide that two schemas with different bytes
agree (rule 2.5), for example because their messages have the same fields with the
same names. Then that reader takes the risk of a wrong meaning. A published
schema never changes its meaning: a new meaning is a new schema. The place of the
schema is the one thing that all variants keep, as all of them keep the place of the
formal of a `Policy`. A message is not read in a schema of its own choice: the reader
first finds the schema in that fixed place.

**Test.** A message needs a schema only if its bytes can reach a reader that did not
get them from its writer through an announced layer, and that reader decides from
what they mean. Do not add a schema to other messages.

## 2. The shape of a formal

**Rule 2.1. One shape.** A thing that a formal defines is a `Descriptor`: `tags` (for
a reader), `prose` (for an implementer) and `formal` (the bytes that nodes compare). An
entry that also needs a value holds a `Descriptor` and the value, never a copy of its
three fields: `Policy`, `Quantity`, `Rate` and `Limit` have this shape. The value (and a
scope) is of each use, so it is never in the descriptor. Use this shape again before
you make a new one. The descriptor files and their rules are in `contract/descriptors/`
(`contract/descriptors/REASON.md`).

**Rule 2.2. Identity is the exact bytes.** Nodes identify an entry only by the bytes
of its `formal`. Tags and prose never identify it. A published formal never changes.
A new meaning is a new formal.

**Rule 2.3. The formal fixes the unit.** The value of an entry is in the unit that
its formal defines. Do not use the formal of one thing for a value in another unit.
(A scarcity in parts per million with the formal of memory, whose unit is bytes, is
an error.) The contract gives no units and no scales.

**Rule 2.4. A property of a thing is in its formal. A behavior applied to a thing is
not.** "Memory is in bytes and is a total, not per core" is a property: it goes in
the formal. "Release this memory when the instance hibernates" is a behavior: it goes
in a policy attached to the entry, never in the formal. If a behavior goes in the
formal, one thing gets two formals, and two nodes no longer compare equal bytes.

**Rule 2.5. Exact bytes by default; reading is a decision of each party.** By
default, a party compares two formals by their exact bytes, and does not look inside
them. A party can also read two formals and decide that they are compatible (for
example when one is a subset of the other). That is a decision of that party, never an
obligation of the contract, and that party carries its risk. Nodo compares the layers
of a peer by exact bytes, and reads them only where it judges a requirement of a
service (for example: is the gateway layer that a service declares a subset of its
own?). A service declares in `Container.node_protocol_stack` only the RPCs of the
gateway that it uses, as a small formal, and not the full layer of the node. So the id
of a service does not change with each release of a node.

A formal does not grow by an edit. A new version or a variant of a formal is a new
formal. Where two parties must keep to talk during a change (the layers of an
address), the new formal is a sibling of the old one, and both are announced while the
old one has users (rule 2.7).

**Rule 2.6. A formal that replaces another names it.** The new formal names the old
one by its id, and says that a list with both is not valid. This is necessary because
some formals hold their values (for example `values=never,on_tunnel`): a new value is
a new formal, and without this rule one list could hold the old and the new one.

**Rule 2.7. An address is a tree.** One address is one point of the network: an ip and
a port. What it speaks is a tree of layers. The roots are the transports of the port.
A layer has more than one child only if the layers up to it can tell the children
apart (for example TLS by ALPN, gRPC by the path of the service), and the formal of
each child includes the value that selects it. An address never repeats itself to
announce more protocols. A requirement (what a party needs) is a path, and a party
looks for its path in the tree of the other party.

**Rule 2.8. An optional protocol has its own file and its own branch.** `celaut.proto`
holds only what two nodes need to work together (rule 1.3). A protocol that a node can
leave out goes in its own `.proto` file (`contract/extensions/`), with its own service,
as one more branch of the tree of the same address, with its own formal. A conflict in
it then does not touch the other branches. A protocol that a celaut service implements
(and not the node) goes through `ServiceTunnel`. `chat.proto` and `observe.proto` are
such protocols: two nodes work together without them, so they are not in
`celaut.proto`.

**Rule 2.9. A descriptor holds what its formal names.** A formal names other formals
by their ids (rule 6.3). `Descriptor.uses` holds them: one `Descriptor` for each id, in
the order of the first occurrence of the id in the formal, each with its own `uses`. So
each `Descriptor` is complete alone, in every message. A reader needs no network, no
catalog and no other part of the message to read it. The order lets a reader match an
entry to its id by the position, with no hash algorithm.

- `uses` is not the identity (rule 2.2). But it is in the bytes of a `Service`, so the id
  of the service covers it.
- A reader checks that `uses` has one entry for each id, and that the digest of each
  formal, with the algorithm of the type of the id, is the id. If not, it refuses the
  descriptor (rule 3.7).
- A document is a descriptor whose formal is the exact bytes of the document. A
  document has no `uses`.
- One descriptor can repeat in the `uses` of many others. This is correct (rule 7.5).

**The test of the probe.** Put the bytes of a service in a probe, and send the probe
into deep space. A reader finds it. That reader knows only ASD-STE100, `celaut.proto`
and the encoding of protobuf, and has no network. Can the reader read each formal, and
build a machine that runs the service? A term that only a person of our world knows
("NVIDIA", "x86-64", "the behavior of Linux") fails the test. Its meaning must be in the
bytes: in a descriptor or in a document in `uses`. To identify a thing is not enough: a
test that a machine passes does not tell how to build that machine.

## 3. Lists

**Rule 3.1. Lists of formals have the same rules.** Every list of entries that a
formal identifies (`Policy`, `Quantity`, `Rate`, `Limit`: the formal of their
descriptor) has these rules: no two entries with the same formal (with the same scope, where the entry has one); the
sender sorts by formal bytes; the order has no meaning; an entry with an empty formal
is not valid. Any other list states its own rules: if its order has a meaning, and if
it can repeat an entry. In a message that has an id or a signature, a reader refuses
a list of formals that is not sorted or that has a repeated formal. Otherwise one
message could have two ids.

**Rule 3.2. The position decides what an unknown formal does, not the type.** The
position depends on who is bound:

- An entry is in a **requirement** for a reader when the entry binds that reader: the
  reader must do what it says, or the reader accepts what it says. A reader refuses
  an entry that it does not know. An old node never opens what an author closed (fail
  closed).
- An entry is in an **announcement** for a reader when the entry only informs that
  reader. A reader keeps an entry that it does not know, and does not use it. A node
  never refuses a peer because the peer knows more (fail open). A report and an
  answer are announcements.
- In both positions, a node can first look for a service that knows the formal
  (rule 1.6). It refuses or ignores only if it finds none, or does not want to look.

The contract states the position of each list for each reader. No type states it.

The rule applies to every party, not only to the node that runs a service. An offer
shows this. Its terms are in two lists, because the caller acts in two ways with no
need to read the formal (rule 3.4):

- A **condition** binds the caller (for example what the node does with the instance
  when its window closes). A caller that does not know a condition does not accept
  the offer.
- A **promise** binds the node (for example a bound on the growth of the price). A
  caller that does not know a promise ignores it. A promise never reduces what the
  caller gets.

The test: if the caller is better off when the term is absent, it is a condition. If
the caller is equal or worse off when it is absent, it is a promise.

The four amounts of `EstimatedCost` let a caller compare the offers of two nodes with
no knowledge of their terms. The node writes the conditions and the promises that it
keeps in the start movement of the instance, so that a caller can show them against
the node.

**Rule 3.3. One comparison rule.** For quantities: more is better. A thing that is
better when it is less is written as its inverse (a latency becomes a rate). Do not
add a direction flag per entry. A `Quantity` is only for a thing that a node compares
with "greater than or equal". A resource is also a thing that adds: the sum of the
values of the instances stays within the capacity. Any other requirement (an exact
value, a relative weight, a feature that is present or not) is a `Policy`, or it is
not in the contract.

**Rule 3.4. Two lists only when the node acts differently.** Make two lists (or two
messages) only if a node must treat the entries differently with no need to read the
formal: it adds one kind and only compares the other, or it charges one and not the
other. Resources and benchmarks are two lists for this reason. If the difference is
only a property, keep one list and put the property in the formal.

**Rule 3.5. Group by role in the place, not by thing.** One thing can have several
facts: an amount, a measured speed, a price. Do not put them in one object. Each place
of the contract needs only some roles: a requirement has an amount and a minimum
score, but no price; a request has only an amount (`ModifyServiceSystemResourcesInput`
has no benchmarks: a score is not a thing that an instance asks for); an announcement
has all three. An
object with all the roles has fields with no meaning in most places (rule 4.2). Also,
facts do not map one to one to things: a score can be about two resources, and a price
can be about an action and not a resource. So each role is its own list, each fact is
in one place, and a formal that is about another formal names it by its id (rule 6.3).
A reader that wants "everything about one thing" joins the lists by the id. That view
is for the reader, not for the contract.

**Rule 3.6. An unknown field number follows rule 3.2.** Protobuf has its own unknown
thing: a field number. In a requirement, the reader refuses: a node does not run a
service that has a field that it does not know. In an announcement, the reader keeps
the field, and the signature covers it (rule 6.2). A node that only stores or
forwards a message keeps all its bytes. For a `Service`, this also keeps its id: the id
is a hash of all its bytes.

A writer can help a reader that does not know a field. For example, the body of a
`ChatMessage` also names the service of `ChatMessage.service`, for a reader that does
not know that field.

**Rule 3.7. A refusal names its cause.** When a node refuses because it does not know
a formal, the answer gives the descriptor of that formal, as the request gave it: in
a field of the answer, or in the details of the error status. Its tags and its prose
help the caller to look. Without this, the caller cannot try another node or
use rule 1.6. Apply it to every refusal where the cause is a formal.

**Rule 3.8. A list of key-value entries is a repeated message, not a map.**
`celaut.proto` has no protobuf `map` field. It has one message for each type of value
(`BytesKeyValue`, `DescriptorKeyValue`), in a `repeated` field. Protobuf does not fix
the order of the entries of a map on the wire: each implementation selects it. The id
of a service is a hash of the serialized `Service`, so two implementations, or two
runs, could write one service as two different sets of bytes, with two ids. A repeated
field keeps the order that it got, and the rules of the list give one order.

History: on the wire, a proto3 map is the same as `repeated Entry { key = 1; value =
2; }` at the same field number. So each field that changed from a map to a list kept
its number and its name. It still reads the bytes that a map wrote, and a node that
still declares the map reads it.

**Rule 3.9. A node announces its capacity for each architecture.** `Peer.resources`
has one `ArchitectureResources` for each architecture that the node runs, because the
resources are different for each architecture. The benchmarks show it most: the same
"1.0 core" is native silicon under one virtualizer and software emulation under
another. A peer uses these entries to judge a `Service.Workload` of a service: the
workload of a descendant can declare only resources, with no hash and no complete
service, so a peer cannot ask `GetServiceEstimatedCost` about it.

## 4. Fields

**Rule 4.1. The field name gives the meaning, not the place.** A comment like "the
location of this message tells if a value is a minimum or a maximum" shows a shape
that is wrong. Give each meaning its own named field.

**Rule 4.2. "Has no meaning here" is a defect.** If a comment says that a field has
no meaning in some place, or is not valid there, change the shape so that the field
cannot be written there.

**Rule 4.3. No magic values.** A value must not trigger an action that it does not
name ("CPU at 0 hibernates the instance"). An action is explicit. Absence and zero
are different: use explicit presence, and say what absence means.

**Rule 4.4. No closed enums for open sets.** An enum is correct only for a set that
is complete by logic (for example yes/no, or the two ends of a stream). If someone
can add a value later, the set is a formal.

**Rule 4.5. Delete, do not deprecate, while in alpha.** A field with no use is
removed. In alpha there is no compatibility to keep, so do not keep debt for it: a
removed field leaves no `reserved` line, and the field numbers of each message stay
compact (1 to n). After the contract is frozen, the contract does not change (rule
7.4).

**Rule 4.6. A field does not fix what a formal defines.** If a formal defines a
thing, no field and no comment of the contract can fix a property of that thing: its
size, its encoding, its unit. For example, a public key "as 64 hexadecimal
characters" fixes a size that only the signature scheme can define. A scheme with
larger keys then breaks the contract. The contract says only "the bytes that the
scheme defines". If the keys of a scheme are too large, the scheme (not the contract)
can define its public key as a digest of the full key.

**Rule 4.7. One message, one meaning.** Two RPCs do not share a message if a field has
a meaning in only one of them. A refusal is a status code or an explicit field, never
a special value of a field with another meaning (for example a token "REFUSED").

## 5. Text in the contract

**Rule 5.1. The contract does not name a member of an open set.** A comment and a
field name of the contract do not name a member of an open set (a resource, a
benchmark, a price, a policy value, a behavior, an algorithm). A comment can give a
member only after the words "for example", and the sentence must stay true when the
example is removed. A catalog of formals (their names and their bytes) is in the
descriptor files (`contract/descriptors/`), never in the comments of the contract. Search the
comments and the field names for such names when you review a change.

**Rule 5.2. One word, one meaning.** Do not use one word for two things (for example
"volatility" of a price and "volatility" of a score). Do not use two words for one
thing.

**Rule 5.3. Promise only what a node can check.** A node cannot guarantee a score
"for all the life" of an instance. It can say what it measured (a percentile) and
when it compares (at each placement). The contract states the honest form.

**Rule 5.4. A comment gives the rule, not the reason.** The comments of the `.proto`
files are a part of the schema (rule 1.8): each byte of a comment is in the identity
of every service and of every signed message. A change to a comment makes a new
schema, and a new id for every service. So a comment gives only what two parties must
agree on:

- what the field or the message holds, and its form;
- what a reader or a node does with it, and when;
- what an absent value or an empty list means;
- if the place is a requirement or an announcement (rule 3.2).

The reason for a design, the alternatives that were refused, the history, and the
consequences for later designs go in this file, not in a comment. A short example can
stay in a comment if the definition is less exact without it (rule 5.1). Do not put in
a comment a path or a module of an implementation, or the number of a rule of this
file: they can change, and a change of a comment changes the schema. A detached comment
(a comment with a blank line between it and the element) is also a part of the schema:
a note for developers goes in this file, not in a detached comment.

The test: **if a sentence of a comment changes, can two parties act in two ways?** If
yes, it is a rule and it stays. If no, it is a reason, and it goes in this file.

## 6. Numbers, signatures and hashes

**Rule 6.1. Exact numbers only.** Signed or hashed data has no floating-point
values. A ratio is two integers, or a quotient that the reader calculates, not a
fixed-point scale that the contract chooses. A signed or hashed message has no
`float`, `double` or signed integer field. A time, a period or a port is an unsigned
integer.

**Rule 6.2. A signature covers bytes, not a message.** A node signs the exact bytes
of a serialized message, in one envelope (`Signed`) for every signed message. A reader
verifies the bytes as they came, and never serializes a message again to verify it.
So a field that the reader does not know is covered by the signature, and a reader
can verify and forward it. A signature never covers a text that a node builds from
the fields by hand: each such text is one more format that two nodes must share. Only
a message that has an id (a `Service`, a `Filesystem`) needs a canonical encoding
(`docs/CANONICAL_ENCODING.md`).

**Rule 6.3. Text names a formal by its id.** Where a formal must appear in text
(JSON, logs, a CLI, a key in a configuration), use the id of its bytes, never its
tags. The id is `<type>:<digest>`, both in lowercase hexadecimal. The type is the
digest of the empty input, as in `Hash`. A short form is only for a
display.

**Rule 6.4. The contract never fixes a hash algorithm.** A field that holds a hash
says its type (`Hash`: the type and the value). A comment that tells
how a hash is calculated says "with the algorithm of its type", and never names an
algorithm except as an example. An implementation selects its algorithm (nodo uses
blake2b-256); that is a choice of the implementation, not of the contract.

**Rule 6.5. A signed message holds a hash of a credential, never the credential.** A
token and a client_id are credentials. A party can show a balance statement, a receipt
or a conversation to others, for example as evidence of a fault (`FaultEvidence`). So
where such a message names an account or an instance, it holds a hash with its own
prefix: `BalanceStatement.account_ref`, `BalanceMovement.instance_ref`,
`ChatEntry.client_ref`. The party that holds the credential can calculate the hash.

- The name of an execution in a receipt follows the same rule. The client has the
  token, so it can calculate the name. In a tunnel to a descendant, the caller has the
  instance_ref and not the token, so the name comes from the instance_ref.
- No signature covers `ServiceInstance.envs_salt`. So the holder of a start receipt can
  show `StartReceipt.envs_digest` to others and keep the values of the environment
  variables secret. Each wake has a receipt with the same salt as the first one, so
  that a party can compare the digests of all the processes of an instance.

**Rule 6.6. A history that a node signs is a hash chain.** The movements of an account
(`BalanceMovement.prev_hash`) and the entries of a conversation (`ChatEntry.prev_hash`)
make a chain: the hash of each one covers the one before it. So a movement that a
statement showed cannot change later, and a node cannot remove or add an entry in the
middle of a conversation without a break in the chain that a reader can see.

**Rule 6.7. An amount is a decimal text in MU.** `Amount.n` is a decimal integer in a
string, not an integer field, because a balance has no limit. MU has no value of its
own: `ContractRate` gives the value of MU for each payment contract. So a node can get
payments in an asset that is not ERG and keep one common scale. ERG is only the
contract whose rate is the peg, and another ledger declares its own rate. Nodo, on
Ergo, uses 1 MU for 1 nanoERG by default, but the operator can change that default. It
is not a definition of MU.

**Rule 6.8. The identity of a node is a key of its own, on no ledger.** The identity
is not a wallet. A wallet key is bound to what its ledger can verify and spend, so an
identity that is a wallet key can never be another thing. The link to a ledger is an
owner attestation on each reputation proof (`Contract.xattrs`). It costs one
signature, and the node can change, add or remove a wallet with no change to its name.

- One signature scheme for each `Peer`, so one public key. A second key is a second
  identity: it divides the reputation of the node, its deposits and the attribution of
  its payments in two. Signatures across the two keys cannot join them again, because
  a party that needs the link speaks only one of the two schemes. A key in a different
  role (the owner of a proof, the TLS key of each process) is not a second identity: it
  is a hierarchy with one root. A scheme that needs two keypairs (for example a hybrid
  of a classical and a post-quantum scheme) is one scheme, with one descriptor whose
  encoding holds both keys.
- `Peer.payment_contracts` is a list on purpose: it is a menu, and the payer selects
  one item from it.
- The owner attestation (`owner_public_key`, `owner_signature`) is in the xattrs of
  the `Contract` of a reputation proof, not in the `Peer`. The pair attributes a fact
  on a ledger to a peer, and the name of the peer does not have to be an object on the
  ledger. For example, a reputation proof on Ergo names its owner in R7, and R7 is the
  spending clause of the reputation contract (`INPUTS.exists { b.propositionBytes ==
  SELF.R7[Coll[Byte]].get }`). So R7 can hold only an Ergo proposition. If a reader
  read R7 as the id of the peer, the identity of every celaut node would be an Ergo
  key. With the attestation, a reader checks two links: R7 is the attested owner, and
  that owner signed this peer_id. It is the same fact. A reader can still verify it
  from the box of the proof alone, with no request to the node (R9 of the box holds
  the full signed `Peer`). It applies also to the reputation system of a second
  ledger, and no ledger has a privilege.
- The attestation is a part of the proof, not of the `Peer`, because the owner is a
  property of the proof. A node can hold many proofs, and they do not need to have one
  owner. One attestation for each ledger in the announcement cannot describe two proofs
  on one ledger from two different wallets.
- A fault report has `ReputationOpinion.amount` 0, because the score of the same object
  already counts the fault.
- The signature of a `ReputationProof` covers `pow_nonce`, so a node that passes the
  proof on cannot change it.

## 7. Extension points

**Rule 7.1. An extension point is a list of formals in the correct place.** The
contract grows at the second level through lists such as `repeated Policy policies`
or `repeated Quantity`. A new extension point costs one field one time, and can save
many changes to the contract later.

**Rule 7.2. Add an extension point only for a known kind of change.** Add it when a
real case shows that the place will need behavior that the contract cannot know.
Do not add it "in case". A list that nobody uses is a cost: it needs rules, tests and
an answer for unknown entries.

**Rule 7.3. Extension points must agree with each other.** If a behavior is a
policy in one place, it must not be an enum value or a magic value in another place.
One concept has one form in the whole contract.

**Rule 7.4. Check the extension points before the freeze.** Rules 4.5 and 7.2 together
make a trap: a place that has no way to grow at the freeze can never get one. So,
before the freeze, check each message that holds a requirement: it must have a list of
`Policy`, or it must be a message that the node refuses when it has an unknown field
(rule 3.6). Check also each kind of change that is known to come (for example new
transports and new protocols on one port: rule 2.7). Here a known kind of change is
enough reason for an extension point; rule 7.2 forbids only an extension point for a
change that nobody expects. After the freeze, `celaut.proto` does not change: a change
is a new formal, or a new branch of a tree.

**Rule 7.5. The contract does not save bytes.** The contract does not use
dictionaries, references or other tricks to make a message smaller (for example a
layer by the id of a descriptor in place of the descriptor). The same bytes can repeat.
To compress or to remove repeated bytes in storage or on the wire is the work of a
layer above (for example the blocks of bee-rpc, which are not part of the protobuf).

`Buffer.skip` in `buffer.proto` is a part of that layer: the receiver of a block tells
the sender that it holds the block already. It is a field of its own so that a reader
never confuses it with `block`, whose meaning on an inbound stream is already taken
(the start and the end of a block). A peer built before `skip` existed reads it as an
unknown field and ignores it. So a sender adds an empty `chunk` to the `Buffer` that
carries `skip`: such a peer then sees a chunk of zero bytes, and not a `Buffer` with no
field that it knows.

## 8. The process

Follow these steps for each change:

1. **Say the need** in one sentence, with no solution in it.
2. **Name the level** (rule 1.1). Apply the interoperability test, the future test
   and the elemental test (1.3, 1.4, 1.5). For a formal, ask how a node that does
   not know it can find an implementation (1.6).
3. **Look for a shape that exists** (`Policy`, `Quantity`, a list with the same
   rules). Use it again before you make a new one.
4. **Apply the tests** of sections 2 to 6 to the proposal.
5. **Write the best argument against the proposal.** If the argument is good, change
   the proposal. A proposal that has no counter-argument is not examined enough.
6. **Count.** Compare the contract before and after: fields, messages, enum values,
   the names of specific things in comments, and the keys of the layer formals that
   must be equal on both sides (they are part of the contract in practice: if one
   changes, old and new nodes stop to talk). A good change removes at least as much
   as it adds, or it moves content from the contract to the formals.
7. **Record what is open** in `contract/PENDING.md`. A question that is not closed does
   not go into the proto as a decision.

## 9. Signs that a design is wrong

- A comment that says "has no meaning here", "not valid here" or "not used now".
- A comment that names a specific resource, price or behavior.
- A value that triggers an action (a magic zero).
- An enum that someone will want to extend.
- A formal that changes because of a behavior and not because of a meaning.
- The same concept in two forms (a policy in one place, an enum in another).
- A unit or a scale written in the contract.
- A property that all nodes need in all places to treat a whole list in one way, but
  that is only in the formal of each entry. (Then it is a structural difference: make
  two lists, rule 3.4.) A property that only matters for one behavior, which a node
  applies after it knows all the formals, can stay in the formal.
- A field or a comment that fixes a size, an encoding or a unit of a thing that a
  formal defines (rule 4.6).
- An unknown formal with no way to look for its implementation: no tags, no prose,
  or bytes that depend on the node that wrote them (rule 1.6).
- One object that holds every fact about a thing, with fields that most places do
  not use (rule 3.5).
- A variation of an elemental concept made into a second concept: two states for one
  "no process" (rule 1.5).
- An enum value or a field of an elemental concept that also says what the node
  does, when, or at what cost (rule 1.5).
- A signature over a text that a node builds from the fields by hand (rule 6.2).
- A hash algorithm named in a comment or a field name (rule 6.4).
- A `float` or a signed integer in a signed or hashed message (rule 6.1).
- A comment of a `.proto` file that explains why, gives the history, or names a path
  of an implementation or a rule of this file (rule 5.4).
- A refusal that gives only a text for a person, when its cause is a formal (rule
  3.7).
- A requirement with no list of `Policy` and no refusal of unknown fields (rule 7.4).
- A term of an offer that can hurt the caller in the list that the caller can ignore
  (rule 3.2).
- One address announced more than one time to give more protocols (rule 2.7).
- An optional protocol in `celaut.proto` (rule 2.8).
- A field or a reference whose only purpose is to make a message smaller (rule 7.5).
- An id in a formal with no entry in `uses`, or a term whose meaning is only in the
  world of the writer: a name of a brand, of a product, of a program (rule 2.9).
- A message that a third party reads, or that has an id, with no schema (rule 1.8). A
  schema in a message that goes only between two parties.
