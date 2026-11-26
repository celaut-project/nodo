# Kinds of network

The domain of a network that a service uses: all the addresses, a host name with its
ports, a domain of celaut instances, the peers of a proof-of-work network of one
chain.

Class: `domain`.

## Places

`Service.Network.domain`: a requirement for the node that runs the service, and the
request of `ResolveNetwork`. A node that does not know the kind refuses, and the error
gives the descriptor.

## Value

The formal gives the form of the value: for example the host name and its ports, or
the chain and the keys of the proof of work. One descriptor for each kind, with the
member in the value (rule 5.3 of `../REASON.md`).

## Notes

Two services meet on one network only if the bytes of the formal and of the value are
equal. The settings of a network are in `../policies/network/`. What the members speak
is in `Service.Network.protocol_stack` (`../protocols/address/`).
