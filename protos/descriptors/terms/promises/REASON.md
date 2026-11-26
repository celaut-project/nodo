# Promises

A term that binds the node: for example a bound on the growth of the price, the most
that the scarcity multiplier can be, how long the node keeps a hibernated instance
that cannot pay.

Class: `promise`.

## Places

- `EstimatedCost.promises`: in a signed offer, the node is bound. A caller that does
  not know a promise ignores it.
- `Peer.promises`: an announcement of what the node does now.

## Value

The formal gives the form, and what an empty value means. With no promise, the caller
assumes none.
