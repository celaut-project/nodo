"""How this node reaches a peer: the routing backends.

A peer address carries the network it is reached over as part of its transport
(``Peer.Uri.transport.tags``). ``tcp`` is the clearnet; ``tor-onion-v3`` is a Tor v3
onion service. Each network is a :class:`~src.routing.base.RoutingBackend`, and
:mod:`src.routing.registry` picks the backend for an address.

See docs/proposals/onion-routing-tor-backend.md.
"""
