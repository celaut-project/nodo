# Prices of a service

What a service charges a caller for the use of one of its slots: each call, each byte,
a subscription.

Class: `rate`.

## Places

`Service.Api.Slot.rates`: for the callers of the service. A node does not read it and
does not charge it.

## Value

An `Amount` in MU for one unit of the formal. The scope is the name of a method, or
empty for the general rate.
