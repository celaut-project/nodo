# Protocols

What an address speaks, as a tree of layers (REASON 2.7). The roots are the transports
of the port. A layer has more than one child only if it can tell the children apart,
and the formal of each child includes the value that selects it. This folder only
holds two folders:

- `address/`: the layers of an address of a node or of an instance, and what the
  members of a network speak.
- `node_gateway/`: the small formals of the RPCs of the gateway of a node that a
  service uses.
