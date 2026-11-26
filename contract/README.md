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
