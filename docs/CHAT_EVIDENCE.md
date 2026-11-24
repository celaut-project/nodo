# Chat evidence

A node that keeps a conversation signs each message that it stores. A client or an operator
that holds these signatures can show a conversation to others, for example in a complaint,
and the others can check it with no help from any node.

This document says what the signature proves, what it does not prove, and how to use it.

## What is signed

A conversation lives on one node (see "Chat" in [USAGE.md](USAGE.md)). That node signs
every message that it stores, with its identity key, the same key as its `Peer`:

- The messages of the client. The signed entry is the **receipt** of the node: it says that
  the node got the text.
- The answers of the operator. The signed entry says that the operator of the node wrote the
  text.

The signature covers: the sequence, the conversation, the time, the direction, the text, the
hashes and the tags of a shared service, the hash of the message before it, and a hash of the
`client_id`. The exact text is in the comments of `ChatEntry` in `celaut.proto`, and the
node announces it in its celaut-gateway layer (`chat.entry.*`).

The signed text never has the `client_id`. A `client_id` is a credential. So a signed
conversation can be shown to others, and they do not learn it.

## The chain

Each entry has the hash of the entry before it in the same conversation (`prev_hash`). The
first entry has a fixed value that depends on the conversation. So the node cannot remove or
add a message in the middle of a conversation without a break that a reader sees. It cannot
sign two different messages after the same one without giving a client two signed entries
that contradict each other.

The sequence is the position of the message among all the messages of the client. It grows
in each conversation, but it has gaps inside one conversation. This is normal.

## What the signature proves

- The node signed this text, in this conversation, at this time on its clock.
- For a message of the client: the node got it. The client holds the receipt.
- For an answer of the operator: the operator of this node said it. The node cannot say later
  that it did not.
- The messages in the document are in this order, and the node did not change them after it
  signed them.

## What the signature does not prove

- **That the client wrote the text.** The signature binds the node and nobody else. A client
  has no key. A node can invent clients and write both sides of a conversation, so a
  conversation that a node shows is a document that the node wrote about itself. Use a
  conversation as evidence **against** the node that signed it, never in favour of it.
  This is the same rule as for an execution receipt ([EXECUTION_RECEIPTS.md](EXECUTION_RECEIPTS.md)).
- **The time.** The time is the clock of the node. For a date that others trust, put the hash
  of the last entry in a place that has its own time, for example a ledger.
- **That the document is the whole conversation.** A client can export the messages that help
  it and keep the others. The chain is for one conversation, and it shows a gap in it, but a
  client can still choose which conversation to show.
- **The older messages.** The node keeps only `chat.MAX_STORED_MESSAGES_PER_CLIENT` messages
  for a client. If it removed the first ones, the document does not start at the first
  message. `chat_verify` says so. The entries that remain are still signed and chained.
- **That the node answered.** A node can refuse to answer or to give the thread. The receipt
  that the client already holds is what protects the client.

## How to use it

On the node that has the conversation (as the client that wrote to a peer, or as the operator
that answered a client):

```text
nodo chat_export <client_id#number> > conversation.json
```

The file has the id of the node that signed (`node`), the hash of the client (`client_ref`)
and the entries with their signatures. It does not have the `client_id`.

Anyone can check the file. It needs no node, no database and no network:

```text
nodo chat_verify conversation.json
```

The command says if every signature verifies for `node` and if the chain holds. It exits with
an error if not. It also says if the first message is not the start of the conversation.

A node that is a client of a peer keeps the signed entries of that peer in its copy of the
conversation. It checks the signature and the chain of each entry when it copies it. It
refuses an entry that does not verify, and it writes a line in the log. So the copy is
evidence from the time that it is copied.

## For a program that is a client

A program that uses the `Chat` RPC gets the receipt in `ChatAck.entry`, and it reads the other
entries with `GetChatThread`. To check an entry, compute `client_ref` and the hash of the entry
as `src/identity/chat_signature.py` does, and verify the signature of `node` over the payload
`celaut-chat-signature:<peer_id>|<hash>`. Keep the entries: the node can remove the old ones.

A client that wants a key of its own, to sign its messages, is not part of this design. It
would make the other side of a conversation provable too.
