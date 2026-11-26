# Quantities

A `Quantity` is a `Descriptor` plus an integer. The formal gives the thing, its unit,
and how a node measures it. One rule of comparison: more is better.

This folder only holds two folders, one for each class:

- `resources/`: a thing that the instance consumes. The node adds the values of its
  instances, keeps the sum within its capacity, enforces it and charges for it.
- `benchmarks/`: a score that the node measures. The node compares it with the minimum
  of a service, and never adds it.

A node refuses a quantity in a list of the other class. A requirement that is not
"greater than or equal" is a setting (`../policies/`), not a quantity.
