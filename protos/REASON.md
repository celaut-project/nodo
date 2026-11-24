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
price, a signature scheme: each one is one case of an open set.

**Rule 1.5. The elemental test.** Some concepts are not cases of an open set. They
are part of what the contract describes, and the contract cannot describe its basic
objects without them. An instance is a process that runs on a defined container. So
"there is a process" and "there is no process" (hibernated) are elemental: no other
state of the same kind can appear. Keep an elemental concept in the contract, once.
Its variations (the causes, the costs, what the node keeps, what wakes it) are
formals. Ask: "Is this a new kind of thing, or a new way to do a thing that the
contract already has?" Only the first can be a candidate for the contract.

**Rule 1.6. An unknown formal is a question, not an end.** A node does not need to
know every formal. The `tags`, the `prose` and the `formal` bytes of an unknown entry
are enough to look for a celaut service that knows it. Then the node can run that
service and use it. So the network adapts by itself: a node learns a new formal when
it meets it, with no change to the contract and no new release of the node.

Example: a node speaks only one signature scheme. It meets a peer with another scheme
(`Peer.signature_scheme`). It cannot verify the peer by itself. But the scheme is a
formal, so the node can look for a celaut service that verifies signatures of that
scheme, run it, and verify the peer. If the scheme were a field or an enum value, the
node could do nothing: the contract would have to change first.

Consequences:

- Every formal must carry enough to find an implementation: tags and prose for a
  person, and the exact bytes as the search key for a node.
- A thing that a service can do for a node (verify, convert, measure, transport) is
  a good candidate for a formal: the network can supply it.
- The trust moves to the service that the node runs. The node selects that service
  by its content hash, as it selects any other service. The contract does not add a
  trust rule for this.

## 2. The shape of a formal

**Rule 2.1. One shape.** A formal entry has `tags` (for a reader), `prose` (for an
implementer) and `formal` (the bytes that nodes compare), plus a value when the entry
needs one. `Policy` and `Quantity` have this shape. Use this shape again before you
make a new one.

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

## 3. Lists

**Rule 3.1. Every list has the same rules.** No two entries with the same formal
(or the same key). The sender sorts by formal bytes. The order has no meaning. An
entry with an empty formal is not valid. These rules make the encoding canonical, so
that a signature and a hash have one answer.

**Rule 3.2. The position decides what an unknown formal does, not the type.**
- In a requirement or a limit, a node refuses an unknown formal. An old node never
  opens what an author closed (fail closed).
- In an announcement, a node keeps an unknown formal and does not use it. A node
  never refuses a peer because the peer knows more (fail open).
- In both positions, a node can first look for a service that knows the formal
  (rule 1.6). It refuses or ignores only if it finds none, or does not want to look.

Every list states which of the two positions it is in.

The rule applies to every party, not only to the node that runs a service. A caller
that gets an offer is in a requirement position: if the offer has a formal that the
caller does not know (for example what the node does when its window closes), the
caller does not accept the offer.

**Rule 3.3. One comparison rule.** For quantities: more is better. A thing that is
better when it is less is written as its inverse (a latency becomes a rate). Do not
add a direction flag per entry.

**Rule 3.4. Two lists only when the node acts differently.** Make two lists (or two
messages) only if a node must treat the entries differently with no need to read the
formal: it adds one kind and only compares the other, or it charges one and not the
other. Resources and benchmarks are two lists for this reason. If the difference is
only a property, keep one list and put the property in the formal.

**Rule 3.5. Group by role in the place, not by thing.** One thing can have several
facts: an amount, a measured speed, a price. Do not put them in one object. Each place
of the contract needs only some roles: a requirement has an amount and a minimum
score, but no price; a request has only an amount; an announcement has all three. An
object with all the roles has fields with no meaning in most places (rule 4.2). Also,
facts do not map one to one to things: a score can be about two resources, and a price
can be about an action and not a resource. So each role is its own list, each fact is
in one place, and a formal that is about another formal names it by its id (rule 6.3).
A reader that wants "everything about one thing" joins the lists by the id. That view
is for the reader, not for the contract.

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
removed. In alpha there is no compatibility to keep, so do not keep debt for it.
After the contract is frozen, a change is a new formal, never a new field.

**Rule 4.6. A field does not fix what a formal defines.** If a formal defines a
thing, no field and no comment of the contract can fix a property of that thing: its
size, its encoding, its unit. For example, a public key "as 64 hexadecimal
characters" fixes a size that only the signature scheme can define. A scheme with
larger keys then breaks the contract.

## 5. Text in the contract

**Rule 5.1. The contract does not name a member of an open set.** If a comment in
`celaut.proto` names a specific resource, benchmark, price, policy value or
behavior ("memory", "CPU", "hibernate"), the rule in that comment probably belongs to
a formal. Search the comments for such names when you review a change.

**Rule 5.2. One word, one meaning.** Do not use one word for two things (for example
"volatility" of a price and "volatility" of a score). Do not use two words for one
thing.

**Rule 5.3. Promise only what a node can check.** A node cannot guarantee a score
"for all the life" of an instance. It can say what it measured (a percentile) and
when it compares (at each placement). The contract states the honest form.

## 6. Numbers, signatures and hashes

**Rule 6.1. Exact numbers only.** Signed or hashed data has no floating-point
values. A ratio is two integers, or a quotient that the reader calculates, not a
fixed-point scale that the contract chooses.

**Rule 6.2. Everything signed is canonical.** A message that is signed or hashed
follows the list rules (3.1) and `docs/CANONICAL_ENCODING.md`. A change to such a
message changes the signatures and the ids. Record each such change.

**Rule 6.3. Text names a formal by its id.** Where a formal must appear in text
(JSON, logs, a CLI, a key in a configuration), use one fixed id of its bytes, never
its tags.

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
   and the names of specific things in comments. A good change removes at least as
   much as it adds, or it moves content from the contract to the formals.
7. **Record what is open** in `protos/PENDING.md`. A question that is not closed does
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
