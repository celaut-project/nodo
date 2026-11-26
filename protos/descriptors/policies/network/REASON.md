# Settings of a network

How a service uses one of its networks: a template in the value of the domain that the node fills at a start (`${VAR}`), a share of a network, the rules of the firewall.

Class: `setting`.

## Places

`Service.Network.policies`: a requirement for the node that runs the service.

## Value

The formal of each setting gives the form of its value, its default when it is absent,
and its `place`. See `../REASON.md` for the rules of all the settings.

## Notes

A setting that changes the value of the domain at a start makes `ResolveNetwork` refuse the network: the caller gives the network as an instance uses it.
