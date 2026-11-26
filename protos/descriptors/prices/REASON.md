# Prices

A `Rate` is a `Descriptor`, a price in MU, and a scope. The formal names the thing that
is priced and its unit. This folder only holds two folders, one for each party that
charges:

- `node/`: what a node charges for a resource that it reserves or an action that it
  does.
- `service/`: what a service charges a caller for the use of a slot.

Rules for both: a list of rates is an announcement, so a reader keeps a rate that it
does not know and does not use it. A sender does not send a rate of 0. In one list,
two rates do not have the same formal and scope. A rate with a scope replaces the
general rate for that scope.
