# Prices of a node

What a node charges: the reserved resources for each second, the bytes of the
network, the kept state of a hibernated instance, the time of a device, and actions
(a build, a tunnel, a connection, a change of resources, a query of a statement).

Class: `rate`.

## Places

`Peer.rates`: an announcement. The offer (`EstimatedCost`) is the authority for one
instance.

## Value

An `Amount` in MU for one unit of the formal. The scope is in the form that the formal
defines (for example the model fact of a device), or empty for the general rate.

## The formal must give

What is charged (`of`: the id of a resource, or the action), the unit of the base (for
example one byte for one second), and the meaning of the scope (`scope`, or
`scope=none`).
