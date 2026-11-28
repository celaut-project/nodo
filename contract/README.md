# The celaut contract

This folder is the specification of celaut: what all the nodes agree on. It does not
depend on nodo. Another implementation needs only this folder.

| File | What it is |
| --- | --- |
| `celaut.proto` | The contract: the messages and the services that two nodes need to work together. |
| `extensions/` | Optional protocols of a node (`chat.proto`, `observe.proto`), each a branch of the address of the node with its own formal. |
| `buffer.proto` | The `Buffer` of bee-rpc, which `celaut.proto` imports. It is a copy of the file of `bee-rpc-over-grpc-py`: keep the two equal. |
| `descriptors/` | The second level: the descriptor files that the contract uses, one folder for each use, with the rules to write them. |
| `REASON.md` | The rules to reason about the contract. Read it before a change. |
| `PENDING.md` | What is still to do for the redesign of the contract. |

Nodo compiles these files into its Python package `protos/` with
`bash/generate_protos.sh`, and the Rust TUI compiles `celaut.proto` from here
(`src/commands/tui/build.rs`). The formats of nodo only (`pack.proto`, `invite.proto`)
are in `protos/`, not here.

## How to change a `.proto` file

1. Read `REASON.md` first. Each comment is a part of the schema (rule 1.8 of
   `REASON.md`), so a comment gives the rule, not the reason (rule 5.4). The reasons,
   the history and the notes for developers go in `REASON.md`.
2. Write the comments in ASD-STE100 Simplified Technical English. The comment of an
   element says what the element is, and what a sender and a receiver do with it. It
   does not refer to the files of an implementation.
3. Nodo puts the leading comment of each message, field and RPC (the comment
   immediately above it, with no blank line between) in the prose of the
   `celaut-gateway` layer that it announces (`src/identity/protocol_doc.py`). It does
   not announce a detached comment (a comment with a blank line between it and the
   element).
4. After a change, run `bash/generate_protos.sh`. It also writes
   `protos/celaut_doc.binpb`, the descriptor with the comments.
