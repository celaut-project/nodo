# RPCs of the gateway that a service uses

A small formal for each group of RPCs of the gateway of a node that a service can use:
for example change its own resources, or start a dependency.

Class: `layer`.

## Places

`Service.Container.node_protocol_stack`: a requirement, bottom to top. The node reads
the formals and accepts the service if the gateway layer of the path is a subset of
its own (REASON 2.5). An empty list declares that the service uses no RPC of the node.

## Value

None: a layer is a `Descriptor`.

## Notes

The service does not declare the full layer of the node, so the id of a service does
not change with each release of a node.
