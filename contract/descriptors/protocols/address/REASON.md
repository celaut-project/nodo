# Layers of an address

A transport (tcp, udp), and each protocol that it carries (tls, http2, grpc, bee-rpc,
the gateway of a node, an optional protocol such as celaut-observe).

Class: `layer`.

## Places

- `Layer.protocol` in the tree of an address (`Instance.Uri`, the addresses of a
  `Peer`): an announcement of what the address speaks.
- `Service.Network.protocol_stack`: a requirement, bottom to top, of what the members
  of a network speak.

## Value

None: a layer is a `Descriptor`. A new version of a layer is a sibling with its own
formal, announced with the old one while the old one has users.

## Notes

The gateway layer of a node is a wire schema (REASON 2.5): split each rule that two
nodes must agree on into a formal of its own.
