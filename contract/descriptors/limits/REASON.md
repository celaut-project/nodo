# Limits

A supply that a node limits and that is not its capacity: for example the number of
connections or of tunnels of one client, the bytes of the network in a period.

Class: `limit`.

## Places

`Peer.limits`: an announcement. An absent limit is not a promise of no limit: the
terms of the offer are the authority for one instance.

## Value

An integer in the unit of the formal. The scope is in the form that the formal
defines, or empty for the general limit.

## The formal must give

`unit`, for whom (`per`: one client, or all the clients of the node), and the meaning
of the scope.
